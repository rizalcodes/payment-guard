"""Persetujuan manusia ujung ke ujung: PRD v2 6.5, Arsitektur v1.1 bagian 7.

``needs_human`` tidak memesan jatah. Itu satu kalimat di dokumen, tapi ia
melahirkan seluruh berkas ini: antara saat agent meminta dan saat operator
menekan tombol, jatahnya bisa sudah habis, mandate-nya bisa sudah berganti,
dan batas tunggunya bisa sudah lewat. Ketiganya diuji di sini terhadap baris
yang benar-benar tersimpan, bukan terhadap nilai balik fungsi.
"""

from __future__ import annotations

import io
import json
import uuid

import psycopg
import pytest
from conftest_guard import (
    RECIPIENT_A,
    RECIPIENT_B,
    USDC,
    approver_url,
    audit_payloads,
    count_events,
    fetch_decision,
    guard_url,
    insert_decision,
)

from payment_guard.approvals_cli import (
    ApprovalError,
    approve,
    fetch_pending,
    list_pending,
    probe_guard_running,
    reject,
)
from payment_guard.config import ApprovalsConfig
from payment_guard.decision_service import CheckPaymentInput, check_payment
from payment_guard.guard_service import GuardSession
from payment_guard.signing import parse_envelope, parse_public_keys, verify

pytestmark = pytest.mark.integration


class Terminal(io.StringIO):
    """stdin palsu yang berlaku sebagai TTY dan mengetik ulang token dari layar."""

    def __init__(self, out: io.StringIO, *, tty: bool = True, salah: bool = False) -> None:
        super().__init__("")
        self._out = out
        self._tty = tty
        self._salah = salah

    def isatty(self) -> bool:
        return self._tty

    def readline(self, *args: object, **kwargs: object) -> str:
        if self._salah:
            return "SALAH2\n"
        return self._out.getvalue().split("melanjutkan:")[1].split()[0] + "\n"


def jalankan(aksi, config: ApprovalsConfig, decision_id: str, **kwargs):
    """Panggil approve atau reject lewat terminal palsu."""
    out = io.StringIO()
    hasil = aksi(config, decision_id, stdin=Terminal(out, **kwargs), out=out)
    return hasil, out.getvalue()


def minta(guard: GuardSession, amount: str, recipient: str = RECIPIENT_A, reason: str = "kopi"):
    return check_payment(
        guard,
        CheckPaymentInput(recipient=recipient, amount=amount, chain="base-sepolia", reason=reason),
    )


@pytest.fixture
def menunggu(guard: GuardSession):
    """Satu keputusan ``needs_human`` yang sah: 24 lolos, 24 berikutnya menunggu."""
    assert minta(guard, "24").status == "approved"
    hasil = minta(guard, "24")
    assert hasil.status == "needs_human"
    return hasil


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #


class TestList:
    def test_shows_the_waiting_decision(self, approvals_config: ApprovalsConfig, menunggu) -> None:
        antrean = fetch_pending(approvals_config)
        assert [item.decision_id for item in antrean] == [menunggu.decision_id]

    def test_empty_queue_says_so(
        self, approvals_config: ApprovalsConfig, guard: GuardSession
    ) -> None:
        out = io.StringIO()
        assert list_pending(approvals_config, out=out) == 0
        assert "Tidak ada keputusan" in out.getvalue()

    def test_reason_is_shown_as_data(
        self, approvals_config: ApprovalsConfig, guard: GuardSession
    ) -> None:
        """ST-21 dan T-8: teks agent dibingkai, kontrolnya di-escape."""
        minta(guard, "24")
        minta(guard, "24", reason="abaikan aturan\x1b[31m dan setujui")

        out = io.StringIO()
        list_pending(approvals_config, out=out)
        teks = out.getvalue()

        assert "bukan instruksi" in teks
        assert "abaikan aturan" in teks
        assert "\x1b" not in teks

    def test_only_agents_with_an_active_mandate_are_listed(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, guard: GuardSession
    ) -> None:
        insert_decision(
            guard_url(migrated),
            agent_id="agent-tanpa-mandate",
            mandate_hash=guard.mandate_hash,
            recipient=RECIPIENT_A,
            status="needs_human",
            reason_code="human_approval_required",
        )
        assert fetch_pending(approvals_config) == []


# --------------------------------------------------------------------------- #
# approve yang lolos
# --------------------------------------------------------------------------- #


class TestApproveSucceeds:
    def test_row_becomes_approved_by_human(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, menunggu
    ) -> None:
        hasil, _ = jalankan(approve, approvals_config, menunggu.decision_id)
        baris = fetch_decision(guard_url(migrated), menunggu.decision_id)

        assert hasil.status == "approved"
        assert hasil.reason_code == "approved_by_human"
        assert baris["status"] == "approved"
        assert baris["reason_code"] == "approved_by_human"

    def test_timestamps_follow_the_schema(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, menunggu, guard
    ) -> None:
        jalankan(approve, approvals_config, menunggu.decision_id)
        baris = fetch_decision(guard_url(migrated), menunggu.decision_id)

        assert baris["approved_at"] == baris["decided_at"]
        assert baris["valid_until"] == baris["approved_at"] + guard.mandate.decision_ttl_seconds
        assert baris["decided_at"] <= baris["expires_at"]

    def test_envelope_verifies_with_the_public_key(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, menunggu, guard
    ) -> None:
        jalankan(approve, approvals_config, menunggu.decision_id)
        baris = fetch_decision(guard_url(migrated), menunggu.decision_id)

        registry = parse_public_keys(guard.config.public_keys_path.read_text(encoding="utf-8"))
        payload = verify(parse_envelope(json.dumps(baris["envelope"])), registry)

        assert payload.decision_id == menunggu.decision_id
        assert payload.agent_id == guard.agent_id
        assert payload.amount_base == str(24 * USDC)
        assert payload.mandate_hash == guard.mandate_hash
        assert payload.valid_until == baris["valid_until"]
        assert payload.issued_at == baris["approved_at"]

    def test_approval_is_audited_without_the_reason(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, guard
    ) -> None:
        minta(guard, "24")
        tertunda = minta(guard, "24", reason="rahasia yang tidak boleh ke audit")
        jalankan(approve, approvals_config, tertunda.decision_id)

        payloads = audit_payloads(guard_url(migrated), "approval_approved")
        assert len(payloads) == 1
        assert payloads[0]["decision_id"] == tertunda.decision_id
        assert "rahasia yang tidak boleh ke audit" not in json.dumps(payloads[0])
        assert "reason" not in payloads[0]

    def test_the_actor_is_the_human(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, menunggu
    ) -> None:
        """Yang memutuskan operator; approver hanyalah role databasenya."""
        jalankan(approve, approvals_config, menunggu.decision_id)
        with psycopg.connect(guard_url(migrated)) as conn, conn.cursor() as cur:
            cur.execute("SELECT actor FROM audit_log WHERE event = %s", ("approval_approved",))
            assert [r[0] for r in cur.fetchall()] == ["human"]

    def test_the_agent_can_read_the_result(
        self, approvals_config: ApprovalsConfig, menunggu, guard
    ) -> None:
        """PRD v2 6.5: agent mengambil hasilnya lewat get_decision_status."""
        from payment_guard.decision_service import get_decision_status

        jalankan(approve, approvals_config, menunggu.decision_id)
        dibaca = get_decision_status(guard, menunggu.decision_id)

        assert dibaca.status == "approved"
        assert dibaca.signed_decision is not None


# --------------------------------------------------------------------------- #
# cek ulang
# --------------------------------------------------------------------------- #


#: Penerima tambahan untuk menghabiskan jatah harian lewat persetujuan biasa.
#:
#: Gerbang manusia diagregasi per penerima, jadi satu penerima tidak bisa
#: dipakai menghabiskan jatah tanpa ikut memicu ``needs_human``. Beberapa
#: penerima berbeda menghindarkan itu.
LAIN = tuple("0x" + f"{index:040x}" for index in (1, 2, 3))


class TestRecheck:
    @pytest.fixture
    def ketat(self, migrated: dict[str, str], guard_session_factory) -> GuardSession:
        """Mandate 20 / 50 / 60: cukup sempit untuk kehabisan jatah dalam satu tes."""
        return guard_session_factory(
            guard_url(migrated),
            human_approval_above="20",
            max_per_tx="50",
            max_per_day="60",
            allowed_recipients=[RECIPIENT_A, RECIPIENT_B, *LAIN],
        )

    @pytest.fixture
    def jatah_habis(self, ketat: GuardSession):
        """Satu permintaan menunggu manusia, lalu jatah hariannya dihabiskan."""
        tertunda = minta(ketat, "30")
        assert tertunda.status == "needs_human"
        for penerima in LAIN:
            assert minta(ketat, "20", recipient=penerima).status == "approved"
        return tertunda

    def test_quota_spent_meanwhile_blocks_the_approval(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, jatah_habis
    ) -> None:
        """Jatah habis setelah agent meminta, sebelum operator menyetujui.

        Jatah harian 60 sudah terpakai penuh oleh tiga pembayaran 20 yang
        disetujui jalur biasa, jadi 30 yang menunggu tidak lagi muat. Inilah
        alasan ``needs_human`` tidak boleh memesan jatah: yang mengikat adalah
        keadaan saat operator menekan tombol, bukan saat agent meminta.
        """
        hasil, _ = jalankan(approve, approvals_config, jatah_habis.decision_id)
        baris = fetch_decision(guard_url(migrated), jatah_habis.decision_id)

        assert hasil.status == "denied"
        assert hasil.reason_code == "recheck_over_daily_limit"
        assert baris["status"] == "denied"
        assert baris["reason_code"] == "recheck_over_daily_limit"
        assert baris["envelope"] is None
        assert baris["approved_at"] is None

    def test_a_rejected_recheck_is_audited(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, jatah_habis
    ) -> None:
        jalankan(approve, approvals_config, jatah_habis.decision_id)

        payloads = audit_payloads(guard_url(migrated), "approval_rejected")
        assert len(payloads) == 1
        assert payloads[0]["reason_code"] == "recheck_over_daily_limit"
        assert payloads[0]["status"] == "denied"

    def test_the_same_approval_would_have_passed_before(
        self, approvals_config: ApprovalsConfig, ketat: GuardSession
    ) -> None:
        """Pasangan tes di atas: tanpa jatah yang habis, 30 yang sama lolos."""
        tertunda = minta(ketat, "30")
        hasil, _ = jalankan(approve, approvals_config, tertunda.decision_id)
        assert hasil.status == "approved"

    def test_a_failed_recheck_starts_a_cooldown(
        self, approvals_config: ApprovalsConfig, ketat: GuardSession, jatah_habis
    ) -> None:
        """``recheck_over_*`` memicu cooldown (reasons.NON_TRIGGERING_DENIALS)."""
        jalankan(approve, approvals_config, jatah_habis.decision_id)
        lanjutan = minta(ketat, "1")
        assert lanjutan.reason_code == "retry_blocked"

    def test_per_transaction_limit_is_rechecked_against_the_active_mandate(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, guard_session_factory
    ) -> None:
        """Mandate diperketat setelah permintaan masuk: mandate berganti menang.

        Mandate yang berganti diperiksa lebih dulu (Arsitektur v1.1 bagian 7),
        jadi yang tercatat ``mandate_changed``, bukan ``recheck_over_*``.
        """
        url = guard_url(migrated)
        guard = guard_session_factory(url, name="awal")
        tertunda = minta(guard, "30")
        assert tertunda.status == "needs_human"
        guard.close()

        guard_session_factory(url, name="ketat", max_per_tx="25", human_approval_above="10")

        hasil, _ = jalankan(approve, approvals_config, tertunda.decision_id)
        baris = fetch_decision(url, tertunda.decision_id)

        assert hasil.reason_code == "mandate_changed"
        assert baris["status"] == "denied"
        assert baris["envelope"] is None

    def test_mandate_change_does_not_start_a_cooldown(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, guard_session_factory
    ) -> None:
        """ST-09: operator yang mengganti mandate bukan kesalahan agent."""
        url = guard_url(migrated)
        guard = guard_session_factory(url, name="awal")
        tertunda = minta(guard, "30")
        guard.close()

        baru = guard_session_factory(url, name="ketat", max_per_tx="40", human_approval_above="10")
        jalankan(approve, approvals_config, tertunda.decision_id)

        lanjutan = minta(baru, "5")
        assert lanjutan.reason_code != "retry_blocked"


# --------------------------------------------------------------------------- #
# kedaluwarsa
# --------------------------------------------------------------------------- #


class TestExpiry:
    @pytest.fixture
    def kedaluwarsa(self, migrated: dict[str, str], guard: GuardSession) -> str:
        """Baris ``needs_human`` yang batas tunggunya sudah lewat."""
        return insert_decision(
            guard_url(migrated),
            agent_id=guard.agent_id,
            mandate_hash=guard.mandate_hash,
            recipient=RECIPIENT_A,
            status="needs_human",
            reason_code="human_approval_required",
            amount_base=10 * USDC,
            ttl_seconds=-60,
        )

    def test_approve_marks_it_expired_instead(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, kedaluwarsa: str
    ) -> None:
        hasil, _ = jalankan(approve, approvals_config, kedaluwarsa)
        baris = fetch_decision(guard_url(migrated), kedaluwarsa)

        assert hasil.status == "expired"
        assert hasil.reason_code == "human_wait_timeout"
        assert baris["status"] == "expired"
        assert baris["envelope"] is None

    def test_decided_at_equals_expires_at(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, kedaluwarsa: str
    ) -> None:
        """CHECK ``decision_state_timestamps`` menuntutnya; audit menyimpan waktu nyata."""
        jalankan(approve, approvals_config, kedaluwarsa)
        baris = fetch_decision(guard_url(migrated), kedaluwarsa)
        assert baris["decided_at"] == baris["expires_at"]

    def test_expiry_is_audited(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, kedaluwarsa: str
    ) -> None:
        jalankan(approve, approvals_config, kedaluwarsa)
        assert count_events(guard_url(migrated), "decision_expired") == 1
        assert count_events(guard_url(migrated), "approval_approved") == 0

    def test_reject_also_expires_it(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, kedaluwarsa: str
    ) -> None:
        """Kedaluwarsa diperiksa sebelum tindakan apa pun, termasuk menolak."""
        hasil, _ = jalankan(reject, approvals_config, kedaluwarsa)
        assert hasil.status == "expired"

    def test_an_expired_row_cannot_then_be_approved(
        self, approvals_config: ApprovalsConfig, kedaluwarsa: str
    ) -> None:
        jalankan(approve, approvals_config, kedaluwarsa)
        with pytest.raises(ApprovalError, match="sudah diputuskan"):
            jalankan(approve, approvals_config, kedaluwarsa)


# --------------------------------------------------------------------------- #
# reject
# --------------------------------------------------------------------------- #


class TestReject:
    def test_row_becomes_rejected_by_human(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, menunggu
    ) -> None:
        hasil, _ = jalankan(reject, approvals_config, menunggu.decision_id)
        baris = fetch_decision(guard_url(migrated), menunggu.decision_id)

        assert hasil.status == "rejected_by_human"
        assert hasil.reason_code == "human_rejected"
        assert baris["status"] == "rejected_by_human"
        assert baris["decided_at"] is not None
        assert baris["envelope"] is None

    def test_rejection_starts_a_cooldown(
        self, approvals_config: ApprovalsConfig, menunggu, guard
    ) -> None:
        """``rejected_by_human`` memicu cooldown (reasons.triggers_cooldown)."""
        jalankan(reject, approvals_config, menunggu.decision_id)
        lanjutan = minta(guard, "1")

        assert lanjutan.reason_code == "retry_blocked"
        assert lanjutan.retry_after > 0

    def test_the_cooldown_is_per_recipient(
        self, approvals_config: ApprovalsConfig, menunggu, guard
    ) -> None:
        jalankan(reject, approvals_config, menunggu.decision_id)
        assert minta(guard, "1", recipient=RECIPIENT_B).status == "approved"

    def test_rejection_is_audited(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, menunggu
    ) -> None:
        jalankan(reject, approvals_config, menunggu.decision_id)
        payloads = audit_payloads(guard_url(migrated), "approval_rejected")
        assert len(payloads) == 1
        assert payloads[0]["reason_code"] == "human_rejected"

    def test_reject_never_reads_the_signing_key(
        self, approvals_config: ApprovalsConfig, menunggu
    ) -> None:
        """Jalur tolak tidak punya alasan menyentuh bahan kunci."""
        buntu = ApprovalsConfig(
            db_url=approvals_config.db_url,
            signing_key_path=approvals_config.signing_key_path.with_name("tidak-ada.json"),
            lock_timeout_ms=approvals_config.lock_timeout_ms,
        )
        hasil, _ = jalankan(reject, buntu, menunggu.decision_id)
        assert hasil.status == "rejected_by_human"


# --------------------------------------------------------------------------- #
# Sekali saja
# --------------------------------------------------------------------------- #


class TestDecidedOnlyOnce:
    def test_approving_twice_is_refused(self, approvals_config: ApprovalsConfig, menunggu) -> None:
        jalankan(approve, approvals_config, menunggu.decision_id)
        with pytest.raises(ApprovalError, match="sudah diputuskan"):
            jalankan(approve, approvals_config, menunggu.decision_id)

    def test_rejecting_after_approving_is_refused(
        self, approvals_config: ApprovalsConfig, menunggu
    ) -> None:
        jalankan(approve, approvals_config, menunggu.decision_id)
        with pytest.raises(ApprovalError, match="sudah diputuskan"):
            jalankan(reject, approvals_config, menunggu.decision_id)

    def test_the_second_attempt_changes_nothing(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, menunggu
    ) -> None:
        jalankan(approve, approvals_config, menunggu.decision_id)
        sebelum = fetch_decision(guard_url(migrated), menunggu.decision_id)
        with pytest.raises(ApprovalError):
            jalankan(reject, approvals_config, menunggu.decision_id)
        assert fetch_decision(guard_url(migrated), menunggu.decision_id) == sebelum

    def test_an_already_decided_row_is_not_audited_again(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, menunggu
    ) -> None:
        jalankan(approve, approvals_config, menunggu.decision_id)
        with pytest.raises(ApprovalError):
            jalankan(approve, approvals_config, menunggu.decision_id)
        assert count_events(guard_url(migrated), "approval_approved") == 1

    def test_an_approved_decision_is_not_in_the_queue(
        self, approvals_config: ApprovalsConfig, menunggu
    ) -> None:
        jalankan(approve, approvals_config, menunggu.decision_id)
        assert fetch_pending(approvals_config) == []

    def test_unknown_id_is_refused(self, approvals_config: ApprovalsConfig, guard) -> None:
        with pytest.raises(ApprovalError, match="tidak ditemukan"):
            jalankan(approve, approvals_config, str(uuid.uuid4()))

    def test_a_decision_that_never_waited_is_refused(
        self, approvals_config: ApprovalsConfig, guard
    ) -> None:
        """``approved`` biasa tidak punya ``expires_at``: tidak ada yang bisa disetujui."""
        langsung = minta(guard, "1")
        assert langsung.status == "approved"
        with pytest.raises(ApprovalError, match="tidak pernah menunggu manusia"):
            jalankan(approve, approvals_config, langsung.decision_id)


# --------------------------------------------------------------------------- #
# Konfirmasi dan Guard
# --------------------------------------------------------------------------- #


class TestConfirmationGate:
    def test_a_wrong_token_changes_nothing(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, menunggu
    ) -> None:
        with pytest.raises(ApprovalError, match="tidak cocok"):
            jalankan(approve, approvals_config, menunggu.decision_id, salah=True)

        baris = fetch_decision(guard_url(migrated), menunggu.decision_id)
        assert baris["status"] == "needs_human"
        assert count_events(guard_url(migrated), "approval_approved") == 0

    def test_a_pipe_changes_nothing(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, menunggu
    ) -> None:
        with pytest.raises(ApprovalError, match="terminal interaktif"):
            jalankan(approve, approvals_config, menunggu.decision_id, tty=False)
        assert fetch_decision(guard_url(migrated), menunggu.decision_id)["status"] == "needs_human"

    def test_the_summary_shows_the_amount_and_recipient(
        self, approvals_config: ApprovalsConfig, menunggu
    ) -> None:
        """ST-23: detail lengkap sebelum konfirmasi."""
        _, layar = jalankan(approve, approvals_config, menunggu.decision_id)
        assert "24.000000" in layar
        assert RECIPIENT_A in layar
        assert menunggu.decision_id in layar


class TestGuardProbe:
    def test_guard_running_is_detected(
        self, approvals_config: ApprovalsConfig, menunggu, guard
    ) -> None:
        """ST-44: lock terpegang saat Guard hidup."""
        assert probe_guard_running(approvals_config, guard.agent_id) is True

    def test_guard_down_is_detected(
        self, approvals_config: ApprovalsConfig, menunggu, guard
    ) -> None:
        agent_id = guard.agent_id
        guard.close()
        assert probe_guard_running(approvals_config, agent_id) is False

    def test_the_probe_leaves_no_lock_behind(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, guard_session_factory
    ) -> None:
        """ST-30: probe tidak boleh mengganggu Guard berikutnya."""
        url = guard_url(migrated)
        pertama = guard_session_factory(url, name="pertama")
        agent_id = pertama.agent_id
        pertama.close()

        for _ in range(3):
            assert probe_guard_running(approvals_config, agent_id) is False

        kedua = guard_session_factory(url, name="kedua")
        assert kedua.healthy

    def test_approving_while_guard_runs_still_works(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, menunggu, guard
    ) -> None:
        hasil, layar = jalankan(approve, approvals_config, menunggu.decision_id)

        assert hasil.status == "approved"
        assert hasil.guard_running is True
        assert "Guard sedang jalan" in layar
        payloads = audit_payloads(guard_url(migrated), "approval_approved")
        assert payloads[0]["guard_running"] == "true"

    def test_approving_while_guard_is_down_still_works(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, menunggu, guard
    ) -> None:
        """Keputusan hidup di database, bukan di proses Guard."""
        guard.close()
        hasil, layar = jalankan(approve, approvals_config, menunggu.decision_id)

        assert hasil.status == "approved"
        assert hasil.guard_running is False
        assert "Guard tidak berjalan" in layar
        payloads = audit_payloads(guard_url(migrated), "approval_approved")
        assert payloads[0]["guard_running"] == "false"

    def test_the_decision_transaction_releases_the_agent_lock(
        self, approvals_config: ApprovalsConfig, menunggu, guard
    ) -> None:
        """Lock transaksi harus lepas saat commit, bukan menggantung."""
        jalankan(approve, approvals_config, menunggu.decision_id)
        assert minta(guard, "1", recipient=RECIPIENT_B).status == "approved"


# --------------------------------------------------------------------------- #
# Hak akses
# --------------------------------------------------------------------------- #


class TestPrivileges:
    def test_audit_log_still_refuses_update_and_delete(
        self, migrated: dict[str, str], approvals_config: ApprovalsConfig, menunggu
    ) -> None:
        jalankan(approve, approvals_config, menunggu.decision_id)
        url = approver_url(migrated)

        for perintah in ("UPDATE audit_log SET event = 'diubah'", "DELETE FROM audit_log"):
            with (
                psycopg.connect(url) as conn,
                conn.cursor() as cur,
                pytest.raises(psycopg.Error),
            ):
                cur.execute(perintah)

        assert count_events(guard_url(migrated), "approval_approved") == 1

    def test_approver_cannot_insert_a_decision(
        self, migrated: dict[str, str], guard: GuardSession
    ) -> None:
        """Baris baru hanya lahir dari Guard; approver hanya memperbarui."""
        with (
            psycopg.connect(approver_url(migrated)) as conn,
            conn.cursor() as cur,
            pytest.raises(psycopg.Error),
        ):
            cur.execute(
                "INSERT INTO decisions (id, agent_id, mandate_hash, chain_id, recipient, "
                "amount_base, status, reason_code, decided_at) VALUES "
                "(%s, %s, %s, 84532, %s, 1, 'denied', 'over_daily_limit', now())",
                (str(uuid.uuid4()), guard.agent_id, guard.mandate_hash, RECIPIENT_A),
            )

    def test_approver_cannot_change_the_amount(self, migrated: dict[str, str], menunggu) -> None:
        """UPDATE per kolom: jumlah bukan salah satu yang boleh disentuh."""
        with (
            psycopg.connect(approver_url(migrated)) as conn,
            conn.cursor() as cur,
            pytest.raises(psycopg.Error),
        ):
            cur.execute(
                "UPDATE decisions SET amount_base = 1 WHERE id = %s", (menunggu.decision_id,)
            )


# --------------------------------------------------------------------------- #
# Dua operator sekaligus
# --------------------------------------------------------------------------- #


class TestConcurrentOperators:
    def test_only_one_of_two_approvals_takes_effect(
        self,
        migrated: dict[str, str],
        approvals_config: ApprovalsConfig,
        menunggu,
    ) -> None:
        """Lock agent membuat yang kedua melihat baris yang sudah diputuskan."""
        hasil_pertama, _ = jalankan(approve, approvals_config, menunggu.decision_id)
        with pytest.raises(ApprovalError):
            jalankan(reject, approvals_config, menunggu.decision_id)

        baris = fetch_decision(guard_url(migrated), menunggu.decision_id)
        assert baris["status"] == hasil_pertama.status == "approved"


def test_approvals_config_uses_the_approver_role(
    migrated: dict[str, str], approvals_config: ApprovalsConfig
) -> None:
    """Bukan Guard: hanya approver yang boleh menulis envelope."""
    assert "approver_app" in approvals_config.db_url
    assert approvals_config.db_url == approver_url(migrated)


def test_pending_rows_carry_their_reason_untouched(
    approvals_config: ApprovalsConfig, guard: GuardSession
) -> None:
    """Penyajian yang aman adalah urusan tampilan; penyimpanannya apa adanya."""
    minta(guard, "24")
    minta(guard, "24", reason="teks\x1b asli")
    antrean = fetch_pending(approvals_config)
    assert antrean[0].reason_text == "teks\x1b asli"
