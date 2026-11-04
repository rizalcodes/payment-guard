"""Executor: daftar cek, urutannya, dan transaksi yang dibangun dari payload.

PRD v2 bagian 7 dan 9, PRD v2.1 bagian 9, Arsitektur v1.1 bagian 4 dan 6.

Tidak ada jaringan dan tidak ada database di berkas ini. Cek 4, 4b, dan 5
memang membaca tabel, jadi kursornya ditiru oleh :class:`FakeCursor`, yang
hanya mengenali SQL milik Executor dan menolak yang lain. Itu membuat dua hal
bisa diuji yang tidak bisa diuji dengan database sungguhan tanpa kerumitan
besar: **urutan** pernyataan yang dikirim, dan bahwa cek 1 sampai 5 tidak
mengirim satu pun pernyataan tulis.

Envelope di sini ditandatangani sungguhan dengan kunci uji di ``tmp_path``.
Yang ditiru hanya dunia luar, bukan kriptografinya: envelope yang "tidak sah"
harus tidak sah karena byte-nya, bukan karena ada tes yang memaksanya begitu.
"""

from __future__ import annotations

import ast
import base64
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from fake_chain import TEST_WALLET_ADDRESS
from payment_guard import executor
from payment_guard.canonical import canonical_bytes
from payment_guard.chain import Fees, encode_transfer
from payment_guard.db import SELECT_DB_NOW, TAKE_XACT_LOCK, TAKE_XACT_LOCK_SHARED
from payment_guard.events import FORBIDDEN_PAYLOAD_KEYS
from payment_guard.executor import (
    FAILURE_CAUSES,
    REASON_ALREADY_EXECUTED,
    REASON_DECISION_MISMATCH,
    REASON_EXPIRED,
    REASON_MANDATE_NOT_ACTIVE,
    REASON_NOT_APPROVED,
    REASON_SIGNATURE_INVALID,
    REASON_VERSION_OR_CHAIN_MISMATCH,
    REJECTION_REASONS,
    CheckRejected,
    ExecutionPlan,
    ExecutorError,
    build_transaction_plan,
    parse_and_verify,
    require_uuid,
    verify_and_plan,
)
from payment_guard.guard_service import load_signing_identity
from payment_guard.keygen import run_keygen
from payment_guard.signing import SIGNING_DOMAIN_PREFIX, parse_public_keys, sign
from payment_guard.signing import build_payload as build_signed_payload

MODULE = Path(__file__).resolve().parents[2] / "src" / "payment_guard" / "executor.py"

AGENT_ID = "demo-agent"
DECISION_ID = "123e4567-e89b-42d3-a456-426614174000"
RECIPIENT = "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"
OTHER_RECIPIENT = "0xfb6916095ca1df60bb79ce92ce3ea74c37c5d359"
MANDATE_HASH = "a" * 64
OTHER_MANDATE_HASH = "b" * 64
USDC = "0x036cbd53842c5426634e7929541ec2318f3dcf7e"
CHAIN_ID = 84532
AMOUNT_BASE = 25_000_000
ISSUED_AT = 1_800_000_000
VALID_UNTIL = 1_800_000_900
NOW = 1_800_000_100


# --------------------------------------------------------------------------- #
# Tiruan kursor
# --------------------------------------------------------------------------- #

#: Nama pendek untuk tiap SQL yang Executor kirim, supaya urutannya bisa
#: diperiksa tanpa membandingkan string panjang di dalam asersi.
_LABELS = {
    TAKE_XACT_LOCK_SHARED: "lock_mandate_shared",
    TAKE_XACT_LOCK: "lock_executor",
    SELECT_DB_NOW: "db_now",
    executor.SELECT_DECISION_FOR_EXECUTION: "baca_decisions",
    executor.SELECT_ACTIVE_MANDATE_HASH: "baca_active_mandates",
    executor.SELECT_EXECUTION_STATUS: "baca_executions",
}


#: Penanda "argumen tidak diberikan", supaya ``None`` bisa berarti "tidak ada
#: baris" tanpa bertabrakan dengan nilai bawaan.
_UNSET: Any = object()


class FakeCursor:
    """Kursor yang hanya mengenali SQL milik jalur cek Executor.

    SQL yang tidak dikenal menggagalkan tes alih-alih mengembalikan ``None``
    secara diam-diam. Tanpa itu, kueri baru yang ditambahkan kelak akan lolos
    tanpa pernah diperiksa.
    """

    def __init__(
        self,
        *,
        now: int = NOW,
        decision: tuple[Any, ...] | object | None = _UNSET,
        active_hash: str | None = MANDATE_HASH,
        execution: tuple[str, str] | None = None,
    ) -> None:
        self.now = now
        # ``None`` berarti "tidak ada baris", bukan "pakai bawaan". Versi
        # pertama tiruan ini memakai ``None`` untuk keduanya, sehingga tes
        # "baris decisions tidak ada" diam-diam menguji baris yang ada.
        self.decision = default_decision_row() if decision is _UNSET else decision
        self.active_hash = active_hash
        self.execution = execution
        self.statements: list[str] = []
        self._result: tuple[Any, ...] | None = None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        label = _LABELS.get(sql)
        if label is None:
            raise AssertionError(f"SQL tak terduga dikirim Executor:\n{sql}")
        self.statements.append(label)
        if label == "db_now":
            self._result = (self.now,)
        elif label == "baca_decisions":
            self._result = self.decision
        elif label == "baca_active_mandates":
            self._result = None if self.active_hash is None else (self.active_hash,)
        elif label == "baca_executions":
            self._result = self.execution
        else:
            self._result = None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result


def default_decision_row(**overrides: Any) -> tuple[Any, ...]:
    """Baris ``decisions`` yang cocok dengan envelope bawaan di berkas ini."""
    nilai: dict[str, Any] = {
        "agent_id": AGENT_ID,
        "recipient": RECIPIENT,
        "amount_base": AMOUNT_BASE,
        "mandate_hash": MANDATE_HASH,
        "chain_id": CHAIN_ID,
        "status": "approved",
        "valid_until": VALID_UNTIL,
    }
    nilai.update(overrides)
    return tuple(nilai.values())


# --------------------------------------------------------------------------- #
# Envelope uji
# --------------------------------------------------------------------------- #


@pytest.fixture
def keys(tmp_path: Path) -> dict[str, Path]:
    """Pasangan kunci uji di ``tmp_path``, di luar akar repo."""
    signing_path = tmp_path / "di-luar-repo" / "guard_signing.json"
    public_path = tmp_path / "guard_public.json"
    run_keygen(signing_path, public_path)
    return {"signing": signing_path, "public": public_path}


@pytest.fixture
def registry(keys: dict[str, Path]) -> dict:
    return parse_public_keys(keys["public"].read_text(encoding="utf-8"))


def envelope_text(keys: dict[str, Path], **overrides: Any) -> str:
    """Envelope sah, ditandatangani kunci uji, sebagai teks JSON."""
    identity = load_signing_identity(keys["signing"])
    nilai: dict[str, Any] = {
        "decision_id": DECISION_ID,
        "mandate_id": "m-001",
        "agent_id": AGENT_ID,
        "recipient": RECIPIENT,
        "amount_base": AMOUNT_BASE,
        "issued_at": ISSUED_AT,
        "valid_until": VALID_UNTIL,
        "mandate_hash": MANDATE_HASH,
    }
    nilai.update(overrides)
    envelope = sign(build_signed_payload(**nilai), identity.signer, identity.key_id)
    return json.dumps(envelope.as_dict())


def hand_signed_envelope(keys: dict[str, Path], **payload_overrides: Any) -> str:
    """Envelope yang ditandatangani di luar :func:`build_payload`.

    Dibutuhkan untuk menguji nilai yang ``build_payload`` sendiri menolak
    membuat, mis. ``status`` selain ``approved`` atau ``v`` yang salah. Tanda
    tangannya **sah**: ia dihitung atas byte kanonik payload yang sama persis,
    jadi yang diuji benar-benar cek nilai (cek 2), bukan cek tanda tangan.
    """
    identity = load_signing_identity(keys["signing"])
    payload: dict[str, Any] = {
        "v": "payment-guard/v1",
        "decision_id": DECISION_ID,
        "mandate_id": "m-001",
        "agent_id": AGENT_ID,
        "chain_id": CHAIN_ID,
        "recipient": RECIPIENT,
        "amount_base": str(AMOUNT_BASE),
        "status": "approved",
        "issued_at": ISSUED_AT,
        "valid_until": VALID_UNTIL,
        "mandate_hash": MANDATE_HASH,
    }
    payload.update(payload_overrides)
    signature = identity.signer.sign(SIGNING_DOMAIN_PREFIX + canonical_bytes(payload))
    return json.dumps(
        {
            "payload": payload,
            "key_id": identity.key_id,
            "signature": base64.b64encode(signature).decode("ascii"),
        }
    )


def plan_from(keys: dict[str, Path], registry: dict, cursor: FakeCursor) -> ExecutionPlan:
    return verify_and_plan(
        envelope_text(keys), cur=cursor, expected_chain_id=CHAIN_ID, registry=registry
    )


def expect_rejection(text: str, registry: dict, cursor: FakeCursor | None = None) -> CheckRejected:
    """Jalankan cek dan kembalikan penolakannya."""
    with pytest.raises(CheckRejected) as info:
        verify_and_plan(
            text,
            cur=cursor if cursor is not None else FakeCursor(),
            expected_chain_id=CHAIN_ID,
            registry=registry,
        )
    return info.value


# --------------------------------------------------------------------------- #
# Jalur yang lolos
# --------------------------------------------------------------------------- #


class TestAllChecksPass:
    def test_plan_carries_the_payload_fields(self, keys: dict[str, Path], registry: dict) -> None:
        plan = plan_from(keys, registry, FakeCursor())
        assert plan.decision_id == DECISION_ID
        assert plan.agent_id == AGENT_ID
        assert plan.recipient == RECIPIENT
        assert plan.amount_base == AMOUNT_BASE
        assert plan.mandate_hash == MANDATE_HASH
        assert plan.chain_id == CHAIN_ID
        assert plan.valid_until == VALID_UNTIL

    def test_plan_carries_the_database_clock(self, keys: dict[str, Path], registry: dict) -> None:
        """``now`` berasal dari ``SELECT ... now()``, bukan dari jam proses."""
        plan = plan_from(keys, registry, FakeCursor(now=1_799_999_999))
        assert plan.now == 1_799_999_999

    def test_amount_is_an_integer_not_a_string(self, keys: dict[str, Path], registry: dict) -> None:
        plan = plan_from(keys, registry, FakeCursor())
        assert isinstance(plan.amount_base, int)


class TestLockOrder:
    """Arsitektur v1.1 bagian 4: lock ``mandate:`` sebelum tabel apa pun."""

    def test_the_mandate_lock_is_the_first_statement(
        self, keys: dict[str, Path], registry: dict
    ) -> None:
        cursor = FakeCursor()
        plan_from(keys, registry, cursor)
        assert cursor.statements[0] == "lock_mandate_shared"

    def test_no_table_is_read_before_the_lock(self, keys: dict[str, Path], registry: dict) -> None:
        cursor = FakeCursor()
        plan_from(keys, registry, cursor)
        pembacaan = [s for s in cursor.statements if s.startswith("baca_")]
        assert cursor.statements.index("lock_mandate_shared") < cursor.statements.index(
            pembacaan[0]
        )

    def test_the_documented_check_order(self, keys: dict[str, Path], registry: dict) -> None:
        cursor = FakeCursor()
        plan_from(keys, registry, cursor)
        assert cursor.statements == [
            "lock_mandate_shared",
            "db_now",
            "baca_decisions",
            "baca_executions",
            "baca_active_mandates",
        ]


class TestNoSideEffectsBeforeCheckSix:
    """Brief M5: tanpa efek samping sampai cek 5 lolos."""

    def test_only_reads_and_one_lock_are_issued(
        self, keys: dict[str, Path], registry: dict
    ) -> None:
        cursor = FakeCursor()
        plan_from(keys, registry, cursor)
        assert all(
            s.startswith("baca_") or s in {"db_now", "lock_mandate_shared"}
            for s in cursor.statements
        ), cursor.statements

    def test_no_insert_or_update_sql_reaches_the_cursor(
        self, keys: dict[str, Path], registry: dict
    ) -> None:
        """Kursor tiruan menolak SQL yang tidak dikenal, termasuk tulis.

        Jadi kalau jalur cek suatu hari menyisipkan INSERT atau UPDATE, tes ini
        gagal dengan menyebut SQL-nya, bukan lolos diam-diam.
        """
        cursor = FakeCursor()
        plan_from(keys, registry, cursor)
        assert executor.INSERT_EXECUTION not in _LABELS
        assert executor.UPDATE_EXECUTION_BROADCAST not in _LABELS
        assert executor.UPDATE_EXECUTION_STATUS not in _LABELS

    def test_a_failed_check_still_issues_no_writes(
        self, keys: dict[str, Path], registry: dict
    ) -> None:
        cursor = FakeCursor(execution=("broadcast", "0x" + "c" * 64))
        expect_rejection(envelope_text(keys), registry, cursor)
        assert all(
            s.startswith("baca_") or s in {"db_now", "lock_mandate_shared"}
            for s in cursor.statements
        )


# --------------------------------------------------------------------------- #
# Cek 1 dan 3: tanda tangan dan bentuk
# --------------------------------------------------------------------------- #


class TestCheckOneSignature:
    def test_unknown_key_id_is_refused(self, keys: dict[str, Path], registry: dict) -> None:
        text = envelope_text(keys)
        dokumen = json.loads(text)
        dokumen["key_id"] = "guard-2020-ffff"
        hasil = expect_rejection(json.dumps(dokumen), registry)
        assert hasil.reason == REASON_SIGNATURE_INVALID
        assert "tidak dikenal" in hasil.detail

    def test_revoked_key_is_refused(self, keys: dict[str, Path], registry: dict) -> None:
        """Runbook 8.1: mencabut kunci harus benar-benar menghentikannya."""
        dicabut = {
            key_id: type(entry)(key_id=entry.key_id, public_key=entry.public_key, revoked=True)
            for key_id, entry in registry.items()
        }
        hasil = expect_rejection(envelope_text(keys), dicabut)
        assert hasil.reason == REASON_SIGNATURE_INVALID
        assert "dicabut" in hasil.detail

    @pytest.mark.parametrize(
        "field,nilai",
        [
            ("amount_base", "99000000"),
            ("recipient", OTHER_RECIPIENT),
            ("mandate_hash", OTHER_MANDATE_HASH),
            ("valid_until", VALID_UNTIL + 1),
            ("agent_id", "agent-lain"),
        ],
    )
    def test_a_tampered_field_breaks_the_signature(
        self, keys: dict[str, Path], registry: dict, field: str, nilai: Any
    ) -> None:
        dokumen = json.loads(envelope_text(keys))
        dokumen["payload"][field] = nilai
        hasil = expect_rejection(json.dumps(dokumen), registry)
        assert hasil.reason == REASON_SIGNATURE_INVALID

    def test_an_envelope_signed_by_another_key_is_refused(
        self, tmp_path: Path, keys: dict[str, Path]
    ) -> None:
        lain = tmp_path / "lain"
        run_keygen(lain / "s.json", lain / "guard_public.json")
        registry_lain = parse_public_keys((lain / "guard_public.json").read_text(encoding="utf-8"))
        hasil = expect_rejection(envelope_text(keys), registry_lain)
        assert hasil.reason == REASON_SIGNATURE_INVALID


class TestCheckThreeStrictParse:
    @pytest.mark.parametrize(
        "rusak",
        [
            "bukan json",
            "{}",
            '{"payload": {}, "key_id": "x", "signature": "y"}',
            '{"payload": {}, "key_id": "x"}',
            "[]",
            '{"payload": 1, "key_id": "a", "signature": "b"}',
        ],
    )
    def test_malformed_envelope_is_signature_invalid(self, registry: dict, rusak: str) -> None:
        """PRD v2.1 bagian 9: parse ketat yang gagal beralasan signature_invalid."""
        hasil = expect_rejection(rusak, registry)
        assert hasil.reason == REASON_SIGNATURE_INVALID

    def test_duplicate_keys_are_refused(self, keys: dict[str, Path], registry: dict) -> None:
        """Dua pembaca JSON tidak boleh bisa melihat dua nilai berbeda."""
        text = envelope_text(keys)
        ganda = text[:-1] + ', "key_id": "guard-2020-aaaa"}'
        hasil = expect_rejection(ganda, registry)
        assert hasil.reason == REASON_SIGNATURE_INVALID

    def test_an_unknown_payload_field_is_refused(
        self, keys: dict[str, Path], registry: dict
    ) -> None:
        dokumen = json.loads(envelope_text(keys))
        dokumen["payload"]["tambahan"] = "x"
        hasil = expect_rejection(json.dumps(dokumen), registry)
        assert hasil.reason == REASON_SIGNATURE_INVALID

    def test_a_float_amount_is_refused(self, keys: dict[str, Path], registry: dict) -> None:
        dokumen = json.loads(envelope_text(keys))
        dokumen["payload"]["chain_id"] = 84532.0
        hasil = expect_rejection(json.dumps(dokumen), registry)
        assert hasil.reason == REASON_SIGNATURE_INVALID


# --------------------------------------------------------------------------- #
# Cek 2: nilai v, chain_id, status
# --------------------------------------------------------------------------- #


class TestCheckTwoValues:
    def test_wrong_chain_id_is_version_or_chain_mismatch(
        self, keys: dict[str, Path], registry: dict
    ) -> None:
        hasil = expect_rejection(hand_signed_envelope(keys, chain_id=1), registry)
        assert hasil.reason == REASON_VERSION_OR_CHAIN_MISMATCH

    def test_wrong_version_is_version_or_chain_mismatch(
        self, keys: dict[str, Path], registry: dict
    ) -> None:
        hasil = expect_rejection(hand_signed_envelope(keys, v="payment-guard/v2"), registry)
        assert hasil.reason == REASON_VERSION_OR_CHAIN_MISMATCH

    @pytest.mark.parametrize("status", ["denied", "needs_human", "expired", "rejected_by_human"])
    def test_a_status_other_than_approved_is_not_approved(
        self, keys: dict[str, Path], registry: dict, status: str
    ) -> None:
        hasil = expect_rejection(hand_signed_envelope(keys, status=status), registry)
        assert hasil.reason == REASON_NOT_APPROVED

    def test_a_valid_signature_over_wrong_values_is_still_refused(
        self, keys: dict[str, Path], registry: dict
    ) -> None:
        """Tanda tangan sah atas pernyataan yang salah tetap salah.

        Envelope di tes ini ditandatangani dengan benar oleh kunci yang
        dikenal. Yang menolaknya adalah nilainya, dan alasannya harus cek 2,
        bukan cek 1 — kalau tertukar, operator akan mengejar kebocoran kunci
        yang tidak pernah terjadi.
        """
        hasil = expect_rejection(hand_signed_envelope(keys, chain_id=11155111), registry)
        assert hasil.reason != REASON_SIGNATURE_INVALID

    def test_the_expected_chain_id_is_the_one_that_is_compared(
        self, keys: dict[str, Path], registry: dict
    ) -> None:
        """Penjaga: ``expected_chain_id`` benar-benar dipakai."""
        with pytest.raises(CheckRejected) as info:
            verify_and_plan(
                envelope_text(keys),
                cur=FakeCursor(),
                expected_chain_id=1,
                registry=registry,
            )
        assert info.value.reason == REASON_VERSION_OR_CHAIN_MISMATCH


# --------------------------------------------------------------------------- #
# Cek 3b: kedaluwarsa
# --------------------------------------------------------------------------- #


class TestCheckThreeBExpiry:
    def test_an_expired_envelope_is_refused(self, keys: dict[str, Path], registry: dict) -> None:
        hasil = expect_rejection(envelope_text(keys), registry, FakeCursor(now=VALID_UNTIL + 1))
        assert hasil.reason == REASON_EXPIRED

    def test_exactly_at_valid_until_is_still_allowed(
        self, keys: dict[str, Path], registry: dict
    ) -> None:
        """``now <= valid_until``, sesuai cek 3 di PRD v2.1 bagian 9."""
        plan = verify_and_plan(
            envelope_text(keys),
            cur=FakeCursor(now=VALID_UNTIL),
            expected_chain_id=CHAIN_ID,
            registry=registry,
        )
        assert plan.now == VALID_UNTIL

    def test_expiry_is_judged_before_the_decision_row(
        self, keys: dict[str, Path], registry: dict
    ) -> None:
        """Envelope kedaluwarsa ditolak tanpa perlu membaca ``decisions``."""
        cursor = FakeCursor(now=VALID_UNTIL + 1, decision=None)
        hasil = expect_rejection(envelope_text(keys), registry, cursor)
        assert hasil.reason == REASON_EXPIRED
        assert "baca_decisions" not in cursor.statements

    def test_the_clock_is_the_database_not_the_process(
        self, keys: dict[str, Path], registry: dict
    ) -> None:
        """Jam proses jauh di masa depan; jam database yang menentukan."""
        plan = verify_and_plan(
            envelope_text(keys),
            cur=FakeCursor(now=ISSUED_AT + 1),
            expected_chain_id=CHAIN_ID,
            registry=registry,
        )
        assert plan.now == ISSUED_AT + 1


# --------------------------------------------------------------------------- #
# Cek 4: identik dengan baris decisions
# --------------------------------------------------------------------------- #


class TestCheckFourDecisionFields:
    def test_a_missing_decision_row_is_refused(self, keys: dict[str, Path], registry: dict) -> None:
        hasil = expect_rejection(envelope_text(keys), registry, FakeCursor(decision=None))
        assert hasil.reason == REASON_DECISION_MISMATCH

    @pytest.mark.parametrize(
        "kolom,nilai",
        [
            ("agent_id", "agent-lain"),
            ("recipient", OTHER_RECIPIENT),
            ("amount_base", AMOUNT_BASE + 1),
            ("mandate_hash", OTHER_MANDATE_HASH),
            ("chain_id", 11155111),
            ("valid_until", VALID_UNTIL + 1),
        ],
    )
    def test_each_field_is_compared(
        self, keys: dict[str, Path], registry: dict, kolom: str, nilai: Any
    ) -> None:
        """Payload tidak dipercaya sendiri; setiap field dicocokkan satu per satu."""
        cursor = FakeCursor(decision=default_decision_row(**{kolom: nilai}))
        hasil = expect_rejection(envelope_text(keys), registry, cursor)
        assert hasil.reason == REASON_DECISION_MISMATCH
        assert kolom in hasil.detail

    def test_amount_is_compared_as_an_integer_not_a_string(
        self, keys: dict[str, Path], registry: dict
    ) -> None:
        """``NUMERIC`` datang sebagai ``Decimal``; "25000000" bukan 25000000."""
        from decimal import Decimal

        cursor = FakeCursor(decision=default_decision_row(amount_base=Decimal(AMOUNT_BASE)))
        plan = plan_from(keys, registry, cursor)
        assert plan.amount_base == AMOUNT_BASE

    @pytest.mark.parametrize("status", ["denied", "needs_human", "expired", "rejected_by_human"])
    def test_a_database_status_other_than_approved_is_refused(
        self, keys: dict[str, Path], registry: dict, status: str
    ) -> None:
        cursor = FakeCursor(decision=default_decision_row(status=status))
        hasil = expect_rejection(envelope_text(keys), registry, cursor)
        assert hasil.reason == REASON_NOT_APPROVED

    def test_the_mismatch_detail_names_every_field_that_differs(
        self, keys: dict[str, Path], registry: dict
    ) -> None:
        cursor = FakeCursor(decision=default_decision_row(recipient=OTHER_RECIPIENT, amount_base=1))
        hasil = expect_rejection(envelope_text(keys), registry, cursor)
        assert "recipient" in hasil.detail
        assert "amount_base" in hasil.detail


# --------------------------------------------------------------------------- #
# Cek 4b: mandate aktif
# --------------------------------------------------------------------------- #


class TestCheckFourBActiveMandate:
    def test_a_missing_active_mandate_row_is_a_failure_not_a_pass(
        self, keys: dict[str, Path], registry: dict
    ) -> None:
        """Tanpa mandate aktif, tidak ada aturan yang sedang berlaku."""
        cursor = FakeCursor(active_hash=None)
        hasil = expect_rejection(envelope_text(keys), registry, cursor)
        assert hasil.reason == REASON_MANDATE_NOT_ACTIVE

    def test_a_replaced_mandate_is_refused(self, keys: dict[str, Path], registry: dict) -> None:
        cursor = FakeCursor(active_hash=OTHER_MANDATE_HASH)
        hasil = expect_rejection(envelope_text(keys), registry, cursor)
        assert hasil.reason == REASON_MANDATE_NOT_ACTIVE
        assert MANDATE_HASH in hasil.detail
        assert OTHER_MANDATE_HASH in hasil.detail

    def test_the_execution_table_is_checked_first(
        self, keys: dict[str, Path], registry: dict
    ) -> None:
        """Cek 5 mendahului cek 4b: replay dilaporkan sebagai replay.

        Ketika keputusan sudah pernah dieksekusi **dan** mandate-nya sudah
        berganti, keduanya benar. Yang harus tercatat adalah
        ``already_executed``: upaya memakai ulang envelope yang sudah terpakai
        adalah sinyal replay, dan melaporkannya sebagai ``mandate_not_active``
        akan menyamarkannya menjadi keluhan tentang aturan.

        Urutan terbalik akan menyembunyikan setiap replay sesudah pergantian
        mandate pertama. Keputusan operator, 4 November 2026.
        """
        cursor = FakeCursor(
            active_hash=OTHER_MANDATE_HASH, execution=("confirmed", "0x" + "d" * 64)
        )
        hasil = expect_rejection(envelope_text(keys), registry, cursor)
        assert hasil.reason == REASON_ALREADY_EXECUTED
        assert "baca_active_mandates" not in cursor.statements


# --------------------------------------------------------------------------- #
# Cek 5: sekali pakai
# --------------------------------------------------------------------------- #


class TestCheckFiveSingleUse:
    @pytest.mark.parametrize("status", ["signed", "broadcast", "confirmed", "failed"])
    def test_any_existing_execution_blocks_a_second_one(
        self, keys: dict[str, Path], registry: dict, status: str
    ) -> None:
        """Termasuk ``failed``: ``UNIQUE(decision_id)`` tidak mengenal pengecualian.

        Keputusan yang eksekusinya gagal tidak bisa dicoba lagi dengan envelope
        yang sama. Itu disengaja — mencoba lagi berarti meminta persetujuan
        baru, bukan memakai ulang yang lama.
        """
        cursor = FakeCursor(execution=(status, "0x" + "e" * 64))
        hasil = expect_rejection(envelope_text(keys), registry, cursor)
        assert hasil.reason == REASON_ALREADY_EXECUTED

    def test_the_detail_names_the_existing_status(
        self, keys: dict[str, Path], registry: dict
    ) -> None:
        cursor = FakeCursor(execution=("broadcast", "0x" + "f" * 64))
        hasil = expect_rejection(envelope_text(keys), registry, cursor)
        assert "broadcast" in hasil.detail


# --------------------------------------------------------------------------- #
# Cek 6: transaksi dibangun dari payload
# --------------------------------------------------------------------------- #


def a_plan(**overrides: Any) -> ExecutionPlan:
    nilai: dict[str, Any] = {
        "decision_id": DECISION_ID,
        "agent_id": AGENT_ID,
        "recipient": RECIPIENT,
        "amount_base": AMOUNT_BASE,
        "mandate_hash": MANDATE_HASH,
        "chain_id": CHAIN_ID,
        "valid_until": VALID_UNTIL,
        "now": NOW,
    }
    nilai.update(overrides)
    return ExecutionPlan(**nilai)


FEES = Fees(max_fee_per_gas=2_000_000_000, max_priority_fee_per_gas=1)


class TestCheckSixBuiltFromPayload:
    def test_calldata_carries_the_payload_recipient_and_amount(self) -> None:
        transaksi = build_transaction_plan(
            a_plan(), usdc_address=USDC, nonce=0, fees=FEES, gas=70_000
        )
        assert transaksi.data == encode_transfer(RECIPIENT, AMOUNT_BASE)

    def test_the_destination_is_the_token_contract(self) -> None:
        transaksi = build_transaction_plan(
            a_plan(), usdc_address=USDC, nonce=0, fees=FEES, gas=70_000
        )
        assert transaksi.to == USDC

    def test_chain_id_comes_from_the_payload(self) -> None:
        transaksi = build_transaction_plan(
            a_plan(chain_id=99), usdc_address=USDC, nonce=0, fees=FEES, gas=70_000
        )
        assert transaksi.chain_id == 99

    def test_value_is_always_zero(self) -> None:
        transaksi = build_transaction_plan(
            a_plan(), usdc_address=USDC, nonce=5, fees=FEES, gas=70_000
        )
        assert transaksi.value == 0

    def test_a_different_recipient_changes_the_calldata(self) -> None:
        satu = build_transaction_plan(a_plan(), usdc_address=USDC, nonce=0, fees=FEES, gas=70_000)
        dua = build_transaction_plan(
            a_plan(recipient=OTHER_RECIPIENT), usdc_address=USDC, nonce=0, fees=FEES, gas=70_000
        )
        assert satu.data != dua.data

    def test_nonce_gas_and_fees_never_touch_the_calldata(self) -> None:
        """Yang datang dari RPC tidak boleh bisa mengubah tujuan atau jumlah."""
        acuan = build_transaction_plan(a_plan(), usdc_address=USDC, nonce=0, fees=FEES, gas=70_000)
        lain = build_transaction_plan(
            a_plan(),
            usdc_address=USDC,
            nonce=999,
            fees=Fees(max_fee_per_gas=1, max_priority_fee_per_gas=1),
            gas=21_000,
        )
        assert acuan.data == lain.data

    def test_there_is_no_parameter_for_a_recipient_or_an_amount(self) -> None:
        """Penjaga bentuk: tidak ada jalan masuk untuk tujuan atau jumlah lain.

        Satu-satunya cara mengirim ke alamat lain adalah mengubah
        ``build_transaction_plan`` sendiri, yang akan terlihat di diff.
        """
        parameter = set(inspect.signature(build_transaction_plan).parameters)
        assert parameter == {"plan", "usdc_address", "nonce", "fees", "gas"}
        for terlarang in ("recipient", "amount", "amount_base", "to", "data"):
            assert terlarang not in parameter

    def test_the_plan_has_no_field_the_payload_does_not_have(self) -> None:
        """``ExecutionPlan`` hanya memuat yang berasal dari payload, plus jam DB."""
        from dataclasses import fields

        assert {f.name for f in fields(ExecutionPlan)} == {
            "decision_id",
            "agent_id",
            "recipient",
            "amount_base",
            "mandate_hash",
            "chain_id",
            "valid_until",
            "now",
        }


# --------------------------------------------------------------------------- #
# Daftar tertutup dan penjaga bentuk
# --------------------------------------------------------------------------- #


class TestClosedLists:
    def test_rejection_reasons_are_unique(self) -> None:
        assert len(set(REJECTION_REASONS)) == len(REJECTION_REASONS)

    def test_rejection_reasons_match_the_prd_table(self) -> None:
        """PRD v2.1 bagian 9: kolom "Alasan penolakan (tertutup)"."""
        assert set(REJECTION_REASONS) == {
            "signature_invalid",
            "version_or_chain_mismatch",
            "not_approved",
            "expired",
            "decision_mismatch",
            "mandate_not_active",
            "already_executed",
        }

    def test_an_unknown_rejection_reason_cannot_be_raised(self) -> None:
        with pytest.raises(ExecutorError):
            raise CheckRejected("alasan-karangan", "apa saja")

    def test_failure_causes_are_only_what_the_chain_answered(self) -> None:
        """PRD v2.1 bagian 9: ``failed`` hanya untuk revert atau nonce terpakai."""
        assert set(FAILURE_CAUSES) == {"receipt_reverted", "nonce_taken"}

    def test_there_is_no_timeout_failure_cause(self) -> None:
        """Menandai ``failed`` karena lambat menyembunyikan pembayaran berhasil."""
        for sebab in FAILURE_CAUSES:
            assert "timeout" not in sebab
            assert "slow" not in sebab
            assert "lambat" not in sebab


class TestRequireUuid:
    def test_a_canonical_uuid_is_accepted(self) -> None:
        require_uuid(DECISION_ID)

    @pytest.mark.parametrize(
        "buruk",
        [
            "",
            "bukan-uuid",
            DECISION_ID.upper(),
            DECISION_ID.replace("-", ""),
            DECISION_ID + "0",
            "123e4567-e89b-42d3-a456-42661417400",
            "{123e4567-e89b-42d3-a456-426614174000}",
        ],
    )
    def test_anything_else_is_refused(self, buruk: str) -> None:
        with pytest.raises(ExecutorError):
            require_uuid(buruk)

    @pytest.mark.parametrize("bukan_str", [None, 1, b"x" * 36])
    def test_non_strings_are_refused(self, bukan_str: object) -> None:
        with pytest.raises(ExecutorError):
            require_uuid(bukan_str)  # type: ignore[arg-type]


class TestParseAndVerifyIsPure:
    """Cek 1, 2, dan 3 tidak butuh database, dan itu bisa diperiksa."""

    def test_it_works_without_a_cursor(self, keys: dict[str, Path], registry: dict) -> None:
        payload = parse_and_verify(
            envelope_text(keys), registry=registry, expected_chain_id=CHAIN_ID
        )
        assert payload.decision_id == DECISION_ID

    def test_it_has_no_cursor_parameter(self) -> None:
        assert "cur" not in inspect.signature(parse_and_verify).parameters

    def test_it_does_not_judge_expiry(self, keys: dict[str, Path], registry: dict) -> None:
        """Kedaluwarsa butuh jam database, jadi ia bukan urusan fungsi ini."""
        payload = parse_and_verify(
            envelope_text(keys, valid_until=ISSUED_AT + 1),
            registry=registry,
            expected_chain_id=CHAIN_ID,
        )
        assert payload.valid_until == ISSUED_AT + 1


# --------------------------------------------------------------------------- #
# Audit: tidak ada teks dari luar yang masuk payload
# --------------------------------------------------------------------------- #


def _audit_payload_keys() -> dict[int, set[str]]:
    """Kunci tiap dict literal yang diberikan ke ``audit(...)`` di executor.py."""
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    found: dict[int, set[str]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "audit":
            continue
        for argument in node.args:
            if isinstance(argument, ast.Dict):
                found[node.lineno] = {
                    key.value
                    for key in argument.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
    return found


class TestAuditPayloads:
    def test_there_are_audit_calls_to_inspect(self) -> None:
        """Penjaga: kalau tidak ada yang ditemukan, tes di bawah kosong."""
        assert _audit_payload_keys()

    def test_no_forbidden_key_is_ever_audited(self) -> None:
        for lineno, keys in _audit_payload_keys().items():
            bocor = keys & FORBIDDEN_PAYLOAD_KEYS
            assert not bocor, f"baris {lineno}: {sorted(bocor)}"

    def test_the_rejection_detail_is_never_audited(self) -> None:
        """``detail`` bisa memuat nilai dari envelope; hanya ``check`` yang dicatat.

        Alasan penolakan adalah enum tertutup milik kita. Detailnya bukan: ia
        memuat alamat, jumlah, dan hash dari envelope yang datang dari luar.
        """
        for lineno, keys in _audit_payload_keys().items():
            for terlarang in ("detail", "message", "error"):
                assert terlarang not in keys, f"baris {lineno}: {terlarang}"

    def test_the_rejection_audit_records_only_the_closed_reason(self) -> None:
        payload = [keys for keys in _audit_payload_keys().values() if "check" in keys]
        assert payload == [{"decision_id", "check"}]


class TestNoKeyMaterialInTheModule:
    """Executor hanya memegang kunci publik Guard dan kunci wallet-nya sendiri."""

    def test_the_module_never_names_the_guard_signing_key_variable(self) -> None:
        """Nama variabel environment saja sudah cukup untuk membaca berkasnya.

        Karena itu yang diperiksa adalah teks sumber, bukan daftar impor. Ada
        pemeriksaan serupa di ``test_source_hygiene.py`` yang berlaku untuk
        ``chain.py`` juga; yang ini menjaga modul ini secara khusus supaya tetap
        gagal walau daftar di berkas itu berubah.
        """
        source = MODULE.read_text(encoding="utf-8")
        assert "GUARD_SIGNING_KEY_PATH" not in source

    @pytest.mark.parametrize(
        "terlarang",
        ["load_signing_identity", "signing_key_path", "Ed25519PrivateKey", "SigningIdentity"],
    )
    def test_the_module_has_no_path_to_signing_material(self, terlarang: str) -> None:
        assert terlarang not in MODULE.read_text(encoding="utf-8")

    def test_the_module_does_not_import_guard_or_approvals(self) -> None:
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        diimpor: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                diimpor.add(node.module)
        for terlarang in ("payment_guard.guard_service", "payment_guard.approvals_cli"):
            assert terlarang not in diimpor

    def test_the_wallet_address_is_not_hardcoded(self) -> None:
        """Alamat wallet datang dari berkas kunci, bukan dari sumber."""
        assert TEST_WALLET_ADDRESS not in MODULE.read_text(encoding="utf-8")
