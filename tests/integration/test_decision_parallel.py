"""ST-13: permintaan paralel tidak boleh menembus batas.

Ini satu-satunya tes yang membuktikan gunanya ``pg_advisory_xact_lock``. Tanpa
lock itu, sepuluh transaksi membaca jatah terpakai yang sama (nol), masing-masing
menyimpulkan masih ada ruang, dan sepuluh pembayaran disetujui padahal jatahnya
cukup untuk tiga. Bug seperti itu **tidak** akan terlihat di tes berurutan: ia
hanya muncul kalau pembacaan dan penulisan benar-benar bertumpang.

Bentuk percobaannya
-------------------

Mandate ``50 / 50 / 100`` dengan sepuluh recipient berbeda di allowlist. Setiap
thread membayar 30 ke recipient-nya sendiri, jadi gerbang manusia (yang
diagregasi per recipient) tidak pernah ikut bicara dan yang tersisa murni batas
harian: ``30 + 30 + 30 = 90`` masuk, yang keempat ``120 > 100`` ditolak.
Jawaban yang benar karena itu **tepat tiga** ``approved``, berapa pun jumlah
thread-nya.

Percobaan diulang dengan jumlah thread berbeda. Bug lock biasanya bergantung
pada waktu: satu jumlah thread bisa kebetulan lulus, lima tidak.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest_guard import (
    RECIPIENT_A,
    USDC,
    count_decisions,
    guard_url,
    sum_approved,
)

from payment_guard.decision_service import CheckPaymentInput, DecisionResult, check_payment
from payment_guard.guard_service import GuardSession

pytestmark = pytest.mark.integration

#: Jumlah thread per percobaan. Lima angka, termasuk satu di bawah jatah (2)
#: dan satu jauh di atasnya (16).
THREAD_COUNTS = (2, 3, 5, 10, 16)

#: Jatah harian cukup untuk tiga pembayaran 30, tidak untuk empat.
EXPECTED_APPROVED = 3

PER_PAYMENT = "30"
DAILY_LIMIT_BASE = 100 * USDC


def recipient_for(index: int) -> str:
    """Alamat sah, satu per thread, supaya agregasi per recipient tidak ikut."""
    return "0x" + f"{index + 1:040x}"


def allowlist(size: int) -> list[str]:
    return [recipient_for(index) for index in range(size)]


def run_in_parallel(guard: GuardSession, threads: int) -> list[DecisionResult]:
    """Jalankan ``threads`` permintaan sekaligus, satu recipient per thread."""

    def minta(index: int) -> DecisionResult:
        return check_payment(
            guard,
            CheckPaymentInput(
                recipient=recipient_for(index),
                amount=PER_PAYMENT,
                chain="base-sepolia",
                reason="paralel",
            ),
        )

    with ThreadPoolExecutor(max_workers=threads) as pool:
        return list(pool.map(minta, range(threads)))


@pytest.fixture
def ketat(
    migrated: dict[str, str], guard_session_factory: Callable[..., GuardSession]
) -> GuardSession:
    """Guard dengan jatah harian 100 dan allowlist cukup untuk semua thread."""
    return guard_session_factory(
        guard_url(migrated),
        human_approval_above="50",
        max_per_tx="50",
        max_per_day="100",
        allowed_recipients=allowlist(max(THREAD_COUNTS)),
    )


class TestParallelDailyLimit:
    """ST-13, lima percobaan dengan jumlah thread berbeda."""

    @pytest.mark.parametrize("threads", THREAD_COUNTS)
    def test_exactly_three_are_approved(
        self, migrated: dict[str, str], ketat: GuardSession, threads: int
    ) -> None:
        url = guard_url(migrated)
        results = run_in_parallel(ketat, threads)

        approved = [r for r in results if r.status == "approved"]
        assert len(approved) == min(threads, EXPECTED_APPROVED), (
            f"{threads} thread: {len(approved)} disetujui, "
            f"status yang keluar: {sorted(r.status for r in results)}"
        )
        assert sum_approved(url, ketat.agent_id) <= DAILY_LIMIT_BASE
        assert count_decisions(url) == threads

    @pytest.mark.parametrize("threads", THREAD_COUNTS)
    def test_the_rest_are_denied_for_the_daily_limit(
        self, ketat: GuardSession, threads: int
    ) -> None:
        """Yang tidak lolos harus ditolak karena jatah, bukan karena error."""
        results = run_in_parallel(ketat, threads)
        ditolak = [r for r in results if r.status != "approved"]

        assert len(ditolak) == max(0, threads - EXPECTED_APPROVED)
        for result in ditolak:
            assert (result.status, result.reason_code) == ("denied", "over_daily_limit")

    def test_every_request_gets_its_own_decision_id(self, ketat: GuardSession) -> None:
        results = run_in_parallel(ketat, max(THREAD_COUNTS))
        identitas = [r.decision_id for r in results]
        assert len(set(identitas)) == len(identitas)

    def test_approved_total_never_exceeds_the_limit(
        self, migrated: dict[str, str], ketat: GuardSession
    ) -> None:
        """Invarian yang sebenarnya dijaga: total disetujui <= batas harian."""
        url = guard_url(migrated)
        run_in_parallel(ketat, max(THREAD_COUNTS))
        assert sum_approved(url, ketat.agent_id) == EXPECTED_APPROVED * 30 * USDC

    def test_a_second_wave_finds_no_quota_left(self, ketat: GuardSession) -> None:
        """Jatah tidak pulih sendiri: gelombang kedua tidak menyetujui apa pun.

        Alasannya tidak seragam, dan itu benar. Tujuh penolakan
        ``over_daily_limit`` dari gelombang pertama **memicu cooldown** untuk
        recipient-nya masing-masing, jadi gelombang kedua ke recipient itu
        dijawab ``retry_blocked`` — cooldown diperiksa di langkah 1, sebelum
        batas harian di langkah 4. Tiga recipient yang tadi disetujui tidak
        punya penolakan pemicu, jadi merekalah yang sampai ke batas harian.
        """
        pertama = run_in_parallel(ketat, 10)
        disetujui = {r.recipient for r in pertama if r.status == "approved"}

        kedua = run_in_parallel(ketat, 10)

        assert not [r for r in kedua if r.status == "approved"]
        for result in kedua:
            diharapkan = "over_daily_limit" if result.recipient in disetujui else "retry_blocked"
            assert result.reason_code == diharapkan, result.recipient


class TestParallelHumanGate:
    """Dua permintaan 24 bersamaan ke recipient yang sama: satu lolos, satu tidak."""

    def test_one_approved_one_needs_human(self, guard: GuardSession) -> None:
        def minta(_: int) -> DecisionResult:
            return check_payment(
                guard,
                CheckPaymentInput(
                    recipient=RECIPIENT_A,
                    amount="24",
                    chain="base-sepolia",
                    reason="paralel",
                ),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(minta, range(2)))

        status = sorted(r.status for r in results)
        assert status == ["approved", "needs_human"]

    def test_the_approved_one_is_the_only_envelope(
        self, migrated: dict[str, str], guard: GuardSession
    ) -> None:
        def minta(_: int) -> DecisionResult:
            return check_payment(
                guard,
                CheckPaymentInput(
                    recipient=RECIPIENT_A,
                    amount="24",
                    chain="base-sepolia",
                    reason="paralel",
                ),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(minta, range(2)))

        bertanda = [r for r in results if r.signed_decision is not None]
        assert len(bertanda) == 1
        assert bertanda[0].status == "approved"
        assert sum_approved(guard_url(migrated), guard.agent_id) == 24 * USDC
