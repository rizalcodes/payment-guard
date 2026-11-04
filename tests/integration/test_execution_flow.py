"""Executor terhadap Postgres sungguhan: write-ahead, broadcast, dan recovery.

PRD v2 bagian 9, PRD v2.1 bagian 9, Arsitektur v1.1 bagian 4 dan 6.

Yang nyata di berkas ini adalah databasenya, tanda tangannya, dan constraint-nya.
Yang ditiru hanya chain, lewat :class:`fake_chain.FakeChain`. Tidak ada tes di
repo ini yang menyentuh jaringan, dan tidak ada transaksi yang pernah disiarkan
ke chain sungguhan — satu-satunya broadcast nyata dikerjakan operator secara
manual lewat ``scripts/demo_execute.md``.

Fixture di berkas ini berasal dari ``conftest_executor.py``, yang **tidak**
menghapus role dan **tidak** menyentuh password role mana pun. Alasannya ada di
kepala berkas itu.
"""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from conftest import RoleNames
from conftest_executor import (
    CHAIN_ID,
    USDC_ADDRESS,
    assert_connects_as,
    audit_events,
    audit_payloads_for,
    count_executions,
    fetch_execution,
    seed_approved_decision,
    seed_mandate,
    set_active_mandate,
)
from conftest_guard import RECIPIENT_A, RECIPIENT_B, USDC
from fake_chain import TEST_WALLET_ADDRESS, TEST_WALLET_SEED, FakeChain
from payment_guard.chain import ChainError
from payment_guard.config import ExecutorConfig
from payment_guard.db import to_int
from payment_guard.executor import (
    NO_NONCE_USED,
    OUTCOME_REJECTED,
    REASON_ALREADY_EXECUTED,
    REASON_EXPIRED,
    REASON_MANDATE_NOT_ACTIVE,
    SELECT_MAX_NONCE,
    STATUS_BROADCAST,
    STATUS_CONFIRMED,
    STATUS_FAILED,
    STATUS_SIGNED,
    execute_decision,
    next_nonce,
    open_runtime,
    recover_pending,
    run_execute,
    start_executor,
)

pytestmark = pytest.mark.integration

AGENT_ID = "demo-agent"
AMOUNT = 10 * USDC

#: Alasan broadcast gagal yang dipakai untuk mencapai keadaan pasca-crash.
#:
#: Keadaan "sudah ditandatangani dan tercatat, tapi belum ada apa pun di
#: jaringan" adalah keadaan yang sama persis dengan crash tepat setelah
#: write-ahead. Mencapainya lewat API publik lebih baik daripada menyuntik
#: baris langsung: yang diuji jadi benar-benar jalur kodenya.
NODE_DOWN = "node tidak bisa dihubungi"


@pytest.fixture
def seeded(executor_db: str, guard_keys: dict[str, Path]) -> dict[str, Any]:
    """Satu mandate aktif dan satu keputusan ``approved`` siap dieksekusi."""
    mandate_hash = seed_mandate(executor_db, agent_id=AGENT_ID)
    decision_id = seed_approved_decision(
        executor_db,
        agent_id=AGENT_ID,
        mandate_hash=mandate_hash,
        recipient=RECIPIENT_A,
        amount_base=AMOUNT,
        signing_key_path=guard_keys["signing"],
    )
    return {"mandate_hash": mandate_hash, "decision_id": decision_id}


@pytest.fixture
def chain() -> FakeChain:
    return FakeChain()


def runtime_for(config: ExecutorConfig, chain: FakeChain):
    return open_runtime(config, client=chain)


def run(config: ExecutorConfig, chain: FakeChain, decision_id: str) -> tuple[Any, str]:
    """Eksekusi satu keputusan dan kembalikan hasilnya beserta keluarannya."""
    out = io.StringIO()
    outcome = execute_decision(runtime_for(config, chain), decision_id, out=out)
    return outcome, out.getvalue()


def recover(config: ExecutorConfig, chain: FakeChain) -> tuple[list[Any], str]:
    out = io.StringIO()
    outcomes = recover_pending(runtime_for(config, chain), out=out)
    return outcomes, out.getvalue()


# --------------------------------------------------------------------------- #
# Jalur bahagia
# --------------------------------------------------------------------------- #


class TestApprovedToConfirmed:
    """Brief M5: approved -> signed -> broadcast -> confirmed."""

    def test_execute_then_recover_reaches_confirmed(
        self, executor_config: ExecutorConfig, executor_db: str, chain: FakeChain, seeded: dict
    ) -> None:
        outcome, _ = run(executor_config, chain, seeded["decision_id"])
        assert outcome.status == STATUS_BROADCAST

        chain.confirm(outcome.tx_hash)
        outcomes, _ = recover(executor_config, chain)
        assert [o.status for o in outcomes] == [STATUS_CONFIRMED]

        row = fetch_execution(executor_db, seeded["decision_id"])
        assert row is not None
        assert row["status"] == STATUS_CONFIRMED

    def test_the_row_records_what_was_signed(
        self, executor_config: ExecutorConfig, executor_db: str, chain: FakeChain, seeded: dict
    ) -> None:
        outcome, _ = run(executor_config, chain, seeded["decision_id"])
        row = fetch_execution(executor_db, seeded["decision_id"])
        assert row is not None
        assert row["tx_hash"] == outcome.tx_hash
        assert row["from_addr"] == TEST_WALLET_ADDRESS
        assert row["to_addr"] == USDC_ADDRESS
        assert to_int(row["amount_base"]) == AMOUNT
        assert row["nonce"] == 0
        assert len(bytes(row["raw_tx"])) > 0

    def test_the_destination_is_the_token_contract_not_the_recipient(
        self, executor_config: ExecutorConfig, executor_db: str, chain: FakeChain, seeded: dict
    ) -> None:
        """``to_addr`` adalah kontrak USDC; penerimanya ada di dalam calldata."""
        run(executor_config, chain, seeded["decision_id"])
        row = fetch_execution(executor_db, seeded["decision_id"])
        assert row is not None
        assert row["to_addr"] != RECIPIENT_A

    def test_the_broadcast_bytes_carry_the_payload_recipient_and_amount(
        self, executor_config: ExecutorConfig, chain: FakeChain, seeded: dict
    ) -> None:
        """Cek 6, diperiksa pada byte yang benar-benar dikirim.

        Calldata tertanam utuh di dalam transaksi ber-RLP, jadi kehadirannya di
        ``raw_tx`` bisa diperiksa tanpa membongkar RLP-nya.
        """
        from payment_guard.chain import encode_transfer

        run(executor_config, chain, seeded["decision_id"])
        assert chain.sent
        assert encode_transfer(RECIPIENT_A, AMOUNT) in chain.sent[0]

    def test_a_different_amount_changes_the_bytes_sent(
        self,
        executor_config: ExecutorConfig,
        executor_db: str,
        chain: FakeChain,
        guard_keys: dict[str, Path],
        seeded: dict,
    ) -> None:
        from payment_guard.chain import encode_transfer

        lain = seed_approved_decision(
            executor_db,
            agent_id=AGENT_ID,
            mandate_hash=seeded["mandate_hash"],
            recipient=RECIPIENT_B,
            amount_base=3 * USDC,
            signing_key_path=guard_keys["signing"],
        )
        run(executor_config, chain, lain)
        assert encode_transfer(RECIPIENT_B, 3 * USDC) in chain.sent[0]
        assert encode_transfer(RECIPIENT_A, AMOUNT) not in chain.sent[0]

    def test_the_audit_trail_is_in_order(
        self, executor_config: ExecutorConfig, executor_db: str, chain: FakeChain, seeded: dict
    ) -> None:
        outcome, _ = run(executor_config, chain, seeded["decision_id"])
        chain.confirm(outcome.tx_hash)
        recover(executor_config, chain)

        kejadian = [e for _, e in audit_events(executor_db) if e.startswith("execution_")]
        assert kejadian == ["execution_signed", "execution_broadcast", "execution_confirmed"]

    def test_every_execution_event_is_recorded_by_the_executor(
        self, executor_config: ExecutorConfig, executor_db: str, chain: FakeChain, seeded: dict
    ) -> None:
        outcome, _ = run(executor_config, chain, seeded["decision_id"])
        chain.confirm(outcome.tx_hash)
        recover(executor_config, chain)

        for actor, event in audit_events(executor_db):
            if event.startswith("execution_"):
                assert actor == "executor", event

    def test_the_signed_event_never_carries_the_envelope(
        self, executor_config: ExecutorConfig, executor_db: str, chain: FakeChain, seeded: dict
    ) -> None:
        """Envelope adalah kredensial sekali pakai (Security v1.1 I5)."""
        run(executor_config, chain, seeded["decision_id"])
        for payload in audit_payloads_for(executor_db, "execution_signed"):
            assert "envelope" not in payload
            assert "signature" not in payload
            assert "raw_tx" not in payload

    def test_no_audit_payload_carries_the_agent_reason(
        self, executor_config: ExecutorConfig, executor_db: str, chain: FakeChain, seeded: dict
    ) -> None:
        """``reason`` hanya boleh hidup di ``decisions.reason_text``."""
        outcome, _ = run(executor_config, chain, seeded["decision_id"])
        chain.confirm(outcome.tx_hash)
        recover(executor_config, chain)

        with psycopg.connect(executor_db) as conn, conn.cursor() as cur:
            cur.execute("SELECT payload::text FROM audit_log")
            semua = " ".join(row[0] for row in cur.fetchall())
        assert "disiapkan tes" not in semua


# --------------------------------------------------------------------------- #
# Write-ahead
# --------------------------------------------------------------------------- #


class TestWriteAhead:
    """PRD v2 bagian 9: baris ``executions`` ada SEBELUM broadcast."""

    def test_the_row_exists_although_nothing_was_sent(
        self, executor_config: ExecutorConfig, executor_db: str, seeded: dict
    ) -> None:
        chain = FakeChain(send_failure=NODE_DOWN)
        outcome, keluaran = run(executor_config, chain, seeded["decision_id"])

        assert chain.sent == [], "tidak boleh ada yang terkirim"
        assert outcome.status == STATUS_SIGNED

        row = fetch_execution(executor_db, seeded["decision_id"])
        assert row is not None
        assert row["status"] == STATUS_SIGNED
        assert row["tx_hash"] == outcome.tx_hash
        assert len(bytes(row["raw_tx"])) > 0
        assert "guard recover" in keluaran

    def test_a_signed_row_has_no_broadcast_timestamp(
        self, executor_config: ExecutorConfig, executor_db: str, seeded: dict
    ) -> None:
        chain = FakeChain(send_failure=NODE_DOWN)
        run(executor_config, chain, seeded["decision_id"])
        row = fetch_execution(executor_db, seeded["decision_id"])
        assert row is not None
        assert row["has_broadcast_at"] is False

    def test_a_broadcast_row_has_a_broadcast_timestamp(
        self, executor_config: ExecutorConfig, executor_db: str, chain: FakeChain, seeded: dict
    ) -> None:
        run(executor_config, chain, seeded["decision_id"])
        row = fetch_execution(executor_db, seeded["decision_id"])
        assert row is not None
        assert row["has_broadcast_at"] is True

    def test_a_failed_broadcast_is_not_a_failed_execution(
        self, executor_config: ExecutorConfig, executor_db: str, seeded: dict
    ) -> None:
        """Node yang tidak bisa dihubungi bukan pembayaran yang gagal."""
        chain = FakeChain(send_failure=NODE_DOWN)
        run(executor_config, chain, seeded["decision_id"])
        row = fetch_execution(executor_db, seeded["decision_id"])
        assert row is not None
        assert row["status"] != STATUS_FAILED

    def test_a_rejected_check_writes_no_execution_row(
        self,
        executor_config: ExecutorConfig,
        executor_db: str,
        chain: FakeChain,
        guard_keys: dict[str, Path],
        seeded: dict,
    ) -> None:
        """Tanpa efek samping sampai cek 5 lolos."""
        kedaluwarsa = seed_approved_decision(
            executor_db,
            agent_id=AGENT_ID,
            mandate_hash=seeded["mandate_hash"],
            recipient=RECIPIENT_A,
            amount_base=AMOUNT,
            signing_key_path=guard_keys["signing"],
            ttl_seconds=900,
            age_seconds=5000,
        )
        outcome, _ = run(executor_config, chain, kedaluwarsa)
        assert outcome.status == OUTCOME_REJECTED
        assert fetch_execution(executor_db, kedaluwarsa) is None
        assert chain.sent == []


# --------------------------------------------------------------------------- #
# Crash dan recovery
# --------------------------------------------------------------------------- #


class TestCrashRecovery:
    """Brief M5 nomor 5: crash setelah write-ahead, lalu satu transaksi saja."""

    def test_recovery_produces_exactly_one_transaction(
        self, executor_config: ExecutorConfig, executor_db: str, seeded: dict
    ) -> None:
        chain = FakeChain(send_failure=NODE_DOWN)
        outcome, _ = run(executor_config, chain, seeded["decision_id"])
        hash_tertulis = outcome.tx_hash
        assert chain.broadcast_count() == 0

        # Node kembali. Recovery menyiarkan byte yang sama.
        chain.send_failure = None
        outcomes, _ = recover(executor_config, chain)

        assert [o.status for o in outcomes] == [STATUS_BROADCAST]
        assert chain.distinct_broadcasts() == 1
        assert count_executions(executor_db) == 1

        row = fetch_execution(executor_db, seeded["decision_id"])
        assert row is not None
        assert row["tx_hash"] == hash_tertulis, "hash tidak boleh berubah saat recovery"
        assert row["status"] == STATUS_BROADCAST

    def test_the_bytes_rebroadcast_are_the_bytes_committed(
        self, executor_config: ExecutorConfig, executor_db: str, seeded: dict
    ) -> None:
        """Membangun ulang transaksi akan memberi hash lain, jadi ia tidak boleh."""
        chain = FakeChain(send_failure=NODE_DOWN)
        run(executor_config, chain, seeded["decision_id"])
        row = fetch_execution(executor_db, seeded["decision_id"])
        assert row is not None

        chain.send_failure = None
        recover(executor_config, chain)
        assert chain.sent == [bytes(row["raw_tx"])]

    def test_recovering_twice_broadcasts_nothing_new(
        self, executor_config: ExecutorConfig, executor_db: str, seeded: dict
    ) -> None:
        chain = FakeChain(send_failure=NODE_DOWN)
        run(executor_config, chain, seeded["decision_id"])
        chain.send_failure = None

        recover(executor_config, chain)
        recover(executor_config, chain)
        recover(executor_config, chain)

        assert chain.distinct_broadcasts() == 1
        assert count_executions(executor_db) == 1

    def test_only_one_broadcast_event_is_audited(
        self, executor_config: ExecutorConfig, executor_db: str, seeded: dict
    ) -> None:
        """``WHERE status = 'signed'`` mencegah kejadian kedua untuk baris sama."""
        chain = FakeChain(send_failure=NODE_DOWN)
        run(executor_config, chain, seeded["decision_id"])
        chain.send_failure = None
        recover(executor_config, chain)
        recover(executor_config, chain)

        kejadian = [e for _, e in audit_events(executor_db)]
        assert kejadian.count("execution_broadcast") == 1

    def test_a_receipt_that_appeared_during_the_crash_is_settled(
        self, executor_config: ExecutorConfig, executor_db: str, seeded: dict
    ) -> None:
        """Crash setelah node menerima transaksi, sebelum status tertulis.

        Baris masih ``signed``, tapi transaksinya sudah masuk blok. Recovery
        harus mencatat broadcast lalu konfirmasi, bukan menyiarkan ulang.
        """
        chain = FakeChain(send_failure=NODE_DOWN)
        outcome, _ = run(executor_config, chain, seeded["decision_id"])

        chain.send_failure = None
        chain.confirm(outcome.tx_hash)
        outcomes, _ = recover(executor_config, chain)

        assert [o.status for o in outcomes] == [STATUS_CONFIRMED]
        assert chain.broadcast_count() == 0, "receipt sudah ada; tidak perlu dikirim lagi"

        row = fetch_execution(executor_db, seeded["decision_id"])
        assert row is not None
        assert row["status"] == STATUS_CONFIRMED
        assert row["has_broadcast_at"] is True, "confirmed wajib punya broadcast_at"

    def test_once_confirmed_there_is_nothing_left_to_recover(
        self, executor_config: ExecutorConfig, chain: FakeChain, seeded: dict
    ) -> None:
        outcome, _ = run(executor_config, chain, seeded["decision_id"])
        chain.confirm(outcome.tx_hash)
        recover(executor_config, chain)

        outcomes, keluaran = recover(executor_config, chain)
        assert outcomes == []
        assert "Tidak ada execution yang tertunda" in keluaran


# --------------------------------------------------------------------------- #
# Kapan ``failed`` ditulis, dan kapan tidak
# --------------------------------------------------------------------------- #


class TestFailureRules:
    """PRD v2.1 bagian 9: ``failed`` hanya untuk revert atau nonce terpakai."""

    def test_a_reverted_receipt_marks_failed(
        self, executor_config: ExecutorConfig, executor_db: str, chain: FakeChain, seeded: dict
    ) -> None:
        outcome, _ = run(executor_config, chain, seeded["decision_id"])
        chain.revert(outcome.tx_hash)
        outcomes, _ = recover(executor_config, chain)

        assert [o.status for o in outcomes] == [STATUS_FAILED]
        row = fetch_execution(executor_db, seeded["decision_id"])
        assert row is not None
        assert row["status"] == STATUS_FAILED

        payloads = audit_payloads_for(executor_db, "execution_failed")
        assert [p["cause"] for p in payloads] == ["receipt_reverted"]

    def test_a_stolen_nonce_marks_failed(
        self, executor_config: ExecutorConfig, executor_db: str, seeded: dict
    ) -> None:
        """Nonce yang sudah dilewati berarti transaksi kita tidak akan masuk."""
        chain = FakeChain(send_failure=NODE_DOWN)
        run(executor_config, chain, seeded["decision_id"])

        chain.send_failure = None
        chain.steal_nonce(0)
        outcomes, _ = recover(executor_config, chain)

        assert [o.status for o in outcomes] == [STATUS_FAILED]
        assert chain.broadcast_count() == 0
        payloads = audit_payloads_for(executor_db, "execution_failed")
        assert [p["cause"] for p in payloads] == ["nonce_taken"]

    def test_a_slow_transaction_is_never_marked_failed(
        self, executor_config: ExecutorConfig, executor_db: str, chain: FakeChain, seeded: dict
    ) -> None:
        """Menandai ``failed`` karena lambat menyembunyikan pembayaran berhasil."""
        run(executor_config, chain, seeded["decision_id"])

        for _ in range(3):
            outcomes, _ = recover(executor_config, chain)
            assert [o.status for o in outcomes] == [STATUS_BROADCAST]

        row = fetch_execution(executor_db, seeded["decision_id"])
        assert row is not None
        assert row["status"] == STATUS_BROADCAST
        assert audit_payloads_for(executor_db, "execution_failed") == []

    def test_an_unreachable_node_during_recovery_is_never_failed(
        self, executor_config: ExecutorConfig, executor_db: str, seeded: dict
    ) -> None:
        chain = FakeChain(send_failure=NODE_DOWN)
        run(executor_config, chain, seeded["decision_id"])

        outcomes, _ = recover(executor_config, chain)
        assert [o.status for o in outcomes] == [STATUS_SIGNED]

        row = fetch_execution(executor_db, seeded["decision_id"])
        assert row is not None
        assert row["status"] == STATUS_SIGNED

    def test_a_failed_execution_still_blocks_a_second_attempt(
        self, executor_config: ExecutorConfig, executor_db: str, chain: FakeChain, seeded: dict
    ) -> None:
        """``UNIQUE(decision_id)`` tidak mengenal pengecualian untuk ``failed``."""
        outcome, _ = run(executor_config, chain, seeded["decision_id"])
        chain.revert(outcome.tx_hash)
        recover(executor_config, chain)

        ulang, _ = run(executor_config, chain, seeded["decision_id"])
        assert ulang.status == OUTCOME_REJECTED
        assert count_executions(executor_db) == 1


# --------------------------------------------------------------------------- #
# Cek yang menolak
# --------------------------------------------------------------------------- #


class TestChecksAgainstTheDatabase:
    def test_the_same_decision_cannot_be_executed_twice(
        self, executor_config: ExecutorConfig, executor_db: str, chain: FakeChain, seeded: dict
    ) -> None:
        pertama, _ = run(executor_config, chain, seeded["decision_id"])
        assert pertama.status == STATUS_BROADCAST

        kedua, keluaran = run(executor_config, chain, seeded["decision_id"])
        assert kedua.status == OUTCOME_REJECTED
        assert REASON_ALREADY_EXECUTED in keluaran
        assert count_executions(executor_db) == 1
        assert chain.distinct_broadcasts() == 1

        payloads = audit_payloads_for(executor_db, "execution_rejected")
        assert [p["check"] for p in payloads] == [REASON_ALREADY_EXECUTED]

    def test_an_expired_envelope_is_refused_and_audited(
        self,
        executor_config: ExecutorConfig,
        executor_db: str,
        chain: FakeChain,
        guard_keys: dict[str, Path],
        seeded: dict,
    ) -> None:
        kedaluwarsa = seed_approved_decision(
            executor_db,
            agent_id=AGENT_ID,
            mandate_hash=seeded["mandate_hash"],
            recipient=RECIPIENT_A,
            amount_base=AMOUNT,
            signing_key_path=guard_keys["signing"],
            ttl_seconds=60,
            age_seconds=3600,
        )
        outcome, keluaran = run(executor_config, chain, kedaluwarsa)

        assert outcome.status == OUTCOME_REJECTED
        assert REASON_EXPIRED in keluaran
        payloads = audit_payloads_for(executor_db, "execution_rejected")
        assert [p["check"] for p in payloads] == [REASON_EXPIRED]
        assert payloads[0]["decision_id"] == kedaluwarsa

    def test_an_envelope_bound_to_a_replaced_mandate_is_refused(
        self, executor_config: ExecutorConfig, executor_db: str, chain: FakeChain, seeded: dict
    ) -> None:
        """Cek 4b: tanda tangan sah atas aturan yang sudah tidak berlaku."""
        baru = seed_mandate(executor_db, agent_id=AGENT_ID, mandate_id="m-002", max_per_tx="40")
        assert baru != seeded["mandate_hash"]
        set_active_mandate(executor_db, agent_id=AGENT_ID, mandate_hash=baru)

        outcome, keluaran = run(executor_config, chain, seeded["decision_id"])

        assert outcome.status == OUTCOME_REJECTED
        assert REASON_MANDATE_NOT_ACTIVE in keluaran
        assert fetch_execution(executor_db, seeded["decision_id"]) is None
        assert chain.sent == []

    def test_a_replay_after_a_mandate_change_is_reported_as_a_replay(
        self, executor_config: ExecutorConfig, executor_db: str, chain: FakeChain, seeded: dict
    ) -> None:
        """Cek 5 mendahului cek 4b, dan di sini itulah yang penting.

        Urutan kejadian: keputusan dieksekusi, lalu operator mengganti mandate,
        lalu envelope yang sama dipakai lagi. Dua cek gagal sekaligus — sudah
        ada execution, dan mandate-nya bukan yang aktif — dan yang harus
        tercatat adalah ``already_executed``.

        Kalau urutannya terbalik, satu pergantian mandate sesudah eksekusi
        akan membuat **setiap** upaya replay sesudahnya tercatat sebagai
        ``mandate_not_active``. Sinyal pemakaian ulang kredensial hilang di
        balik keluhan tentang aturan, dan justru sinyal itu yang ingin dilihat.
        Keputusan operator, 4 November 2026.
        """
        pertama, _ = run(executor_config, chain, seeded["decision_id"])
        assert pertama.status == STATUS_BROADCAST

        baru = seed_mandate(executor_db, agent_id=AGENT_ID, mandate_id="m-003", max_per_tx="30")
        assert baru != seeded["mandate_hash"]
        set_active_mandate(executor_db, agent_id=AGENT_ID, mandate_hash=baru)

        ulang, keluaran = run(executor_config, chain, seeded["decision_id"])

        assert ulang.status == OUTCOME_REJECTED
        assert REASON_ALREADY_EXECUTED in keluaran
        assert REASON_MANDATE_NOT_ACTIVE not in keluaran
        assert count_executions(executor_db) == 1
        assert chain.distinct_broadcasts() == 1

        payloads = audit_payloads_for(executor_db, "execution_rejected")
        assert [p["check"] for p in payloads] == [REASON_ALREADY_EXECUTED]

    def test_a_decision_that_is_not_approved_is_refused(
        self, executor_config: ExecutorConfig, executor_db: str, chain: FakeChain, seeded: dict
    ) -> None:
        """Tanpa envelope tidak ada persetujuan yang bisa dieksekusi."""
        from conftest_guard import insert_decision

        menunggu = insert_decision(
            executor_db,
            agent_id=AGENT_ID,
            mandate_hash=seeded["mandate_hash"],
            recipient=RECIPIENT_A,
            status="needs_human",
            reason_code="human_approval_required",
        )
        outcome, keluaran = run(executor_config, chain, menunggu)
        assert outcome.status == OUTCOME_REJECTED
        assert "not_approved" in keluaran
        assert fetch_execution(executor_db, menunggu) is None

    def test_an_unknown_decision_id_is_an_error(
        self, executor_config: ExecutorConfig, chain: FakeChain, seeded: dict
    ) -> None:
        from payment_guard.executor import ExecutorError

        with pytest.raises(ExecutorError, match="tidak ditemukan"):
            run(executor_config, chain, "00000000-0000-4000-8000-000000000000")

    def test_an_envelope_for_another_agent_is_refused(
        self,
        executor_config: ExecutorConfig,
        executor_db: str,
        chain: FakeChain,
        guard_keys: dict[str, Path],
        seeded: dict,
    ) -> None:
        """Cek 4: field payload harus identik dengan barisnya.

        Envelope di sini ditandatangani dengan benar, tapi ``agent_id``-nya
        tidak sama dengan kolom barisnya. Karena ``agent_id`` juga menentukan
        mandate mana yang dicek, memercayai payload di sini akan memeriksa
        aturan agent yang salah.
        """
        salah = seed_approved_decision(
            executor_db,
            agent_id=AGENT_ID,
            mandate_hash=seeded["mandate_hash"],
            recipient=RECIPIENT_A,
            amount_base=AMOUNT,
            signing_key_path=guard_keys["signing"],
            envelope_overrides={"agent_id": "agent-lain"},
        )
        outcome, keluaran = run(executor_config, chain, salah)
        assert outcome.status == OUTCOME_REJECTED
        assert "agent_id" in keluaran

    def test_an_envelope_with_a_different_amount_is_refused(
        self,
        executor_config: ExecutorConfig,
        executor_db: str,
        chain: FakeChain,
        guard_keys: dict[str, Path],
        seeded: dict,
    ) -> None:
        salah = seed_approved_decision(
            executor_db,
            agent_id=AGENT_ID,
            mandate_hash=seeded["mandate_hash"],
            recipient=RECIPIENT_A,
            amount_base=AMOUNT,
            signing_key_path=guard_keys["signing"],
            envelope_overrides={"amount_base": AMOUNT + 1},
        )
        outcome, keluaran = run(executor_config, chain, salah)
        assert outcome.status == OUTCOME_REJECTED
        assert "amount_base" in keluaran
        assert chain.sent == []


# --------------------------------------------------------------------------- #
# Audit log tetap append-only
# --------------------------------------------------------------------------- #


class TestAuditLogStaysAppendOnly:
    """Security v1.1 A4. Trigger dan hak akses, keduanya diperiksa."""

    @pytest.fixture
    def executed(self, executor_config: ExecutorConfig, chain: FakeChain, seeded: dict) -> dict:
        run(executor_config, chain, seeded["decision_id"])
        return seeded

    def test_update_is_refused(self, executor_db: str, executed: dict) -> None:
        with psycopg.connect(executor_db) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                cur.execute("UPDATE audit_log SET actor = 'guard'")

    def test_delete_is_refused(self, executor_db: str, executed: dict) -> None:
        with psycopg.connect(executor_db) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                cur.execute("DELETE FROM audit_log")

    def test_truncate_is_refused(self, executor_db: str, executed: dict) -> None:
        with psycopg.connect(executor_db) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                cur.execute("TRUNCATE audit_log")

    def test_the_trigger_bites_even_for_the_object_owner(
        self, executor_db: str, executed: dict
    ) -> None:
        """Koneksi di tes ini adalah admin, pemilik objeknya.

        Trigger tetap menolak. Yang **tidak** dilindungi adalah pemilik yang
        sengaja melepas trigger-nya; itu RR-2 di Security v1.1, dan ia memang
        di luar jangkauan mekanisme ini.
        """
        with psycopg.connect(executor_db) as conn, conn.cursor() as cur:
            cur.execute("SELECT current_user")
            row = cur.fetchone()
            assert row is not None and row[0] == "guard_admin"
            with pytest.raises(psycopg.errors.RaiseException):
                cur.execute("DELETE FROM audit_log")


# --------------------------------------------------------------------------- #
# Hak akses role executor
# --------------------------------------------------------------------------- #


class TestExecutorPrivileges:
    def test_the_executor_runs_as_the_executor_role(
        self, executor_config: ExecutorConfig, roles: RoleNames
    ) -> None:
        """Penjaga: tes di kelas ini tidak berjalan dengan hak admin."""
        assert_connects_as(executor_config.db_url, roles.login("executor"))

    def test_those_privileges_are_enough_to_execute(
        self, executor_config: ExecutorConfig, executor_db: str, chain: FakeChain, seeded: dict
    ) -> None:
        """INSERT executions, UPDATE status, dan INSERT audit_log, semuanya cukup."""
        outcome, _ = run(executor_config, chain, seeded["decision_id"])
        chain.confirm(outcome.tx_hash)
        recover(executor_config, chain)

        row = fetch_execution(executor_db, seeded["decision_id"])
        assert row is not None and row["status"] == STATUS_CONFIRMED

    def test_the_executor_cannot_read_the_audit_log(self, executor_config: ExecutorConfig) -> None:
        """``0002_roles.sql``: hanya INSERT pada ``audit_log`` untuk executor.

        Proses yang menulis bukti tidak perlu bisa membacanya kembali.
        """
        with psycopg.connect(executor_config.db_url) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT count(*) FROM audit_log")

    def test_the_executor_cannot_write_decisions(
        self, executor_config: ExecutorConfig, seeded: dict
    ) -> None:
        """Executor tidak pernah mengubah keputusan; ia hanya menjalankannya."""
        with psycopg.connect(executor_config.db_url) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("UPDATE decisions SET status = 'denied'")

    def test_the_executor_cannot_delete_an_execution(
        self, executor_config: ExecutorConfig, chain: FakeChain, seeded: dict
    ) -> None:
        run(executor_config, chain, seeded["decision_id"])
        with psycopg.connect(executor_config.db_url) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("DELETE FROM executions")


# --------------------------------------------------------------------------- #
# Start dan jalur perintah
# --------------------------------------------------------------------------- #


class TestStartup:
    def test_a_healthy_node_prints_what_it_checked(
        self, executor_config: ExecutorConfig, chain: FakeChain
    ) -> None:
        out = io.StringIO()
        _, laporan = start_executor(executor_config, client=chain, out=out)
        keluaran = out.getvalue()
        assert str(CHAIN_ID) in keluaran
        assert USDC_ADDRESS in keluaran
        assert TEST_WALLET_ADDRESS in keluaran
        assert laporan.decimals == 6

    def test_the_rpc_url_is_never_printed_in_full(
        self, executor_url: str, guard_keys: dict[str, Path], tmp_path: Path, chain: FakeChain
    ) -> None:
        """``RPC_URL`` rahasia: penyedia menaruh kunci API di path atau query."""
        from fake_chain import write_test_wallet

        config = ExecutorConfig(
            db_url=executor_url,
            wallet_key_path=write_test_wallet(tmp_path / "luar"),
            public_keys_path=guard_keys["public"],
            rpc_url="https://rpc.example/v2/kunci-sangat-rahasia?token=juga-rahasia",
            chain_id=CHAIN_ID,
            usdc_address=USDC_ADDRESS,
            lock_timeout_ms=5000,
        )
        out = io.StringIO()
        start_executor(config, client=chain, out=out)
        keluaran = out.getvalue()
        assert "kunci-sangat-rahasia" not in keluaran
        assert "juga-rahasia" not in keluaran
        assert "https://rpc.example" in keluaran

    def test_wrong_decimals_refuses_to_start(self, executor_config: ExecutorConfig) -> None:
        with pytest.raises(ChainError, match="decimals"):
            start_executor(executor_config, client=FakeChain(decimals_value=18), out=io.StringIO())

    def test_wrong_chain_id_refuses_to_start(self, executor_config: ExecutorConfig) -> None:
        with pytest.raises(ChainError, match="chain id"):
            start_executor(
                executor_config, client=FakeChain(chain_id_value=8453), out=io.StringIO()
            )

    def test_a_low_gas_balance_only_warns(
        self, executor_config: ExecutorConfig, seeded: dict
    ) -> None:
        """Saldo kurang tidak boleh menghalangi ``recover`` dijalankan."""
        chain = FakeChain(balance_wei_value=1)
        out = io.StringIO()
        start_executor(executor_config, client=chain, out=out)
        assert "peringatan" in out.getvalue()


class TestRunExecute:
    def test_it_recovers_before_executing(
        self,
        executor_config: ExecutorConfig,
        executor_db: str,
        guard_keys: dict[str, Path],
        seeded: dict,
    ) -> None:
        """PRD v2.1 bagian 9: recovery berjalan di awal setiap ``guard execute``.

        Execution yang tertinggal memakai nonce yang sudah dialokasikan;
        menumpuk transaksi baru di atasnya hanya memperbanyak yang harus
        dipulihkan.
        """
        chain = FakeChain(send_failure=NODE_DOWN)
        run(executor_config, chain, seeded["decision_id"])
        assert chain.broadcast_count() == 0

        lain = seed_approved_decision(
            executor_db,
            agent_id=AGENT_ID,
            mandate_hash=seeded["mandate_hash"],
            recipient=RECIPIENT_B,
            amount_base=2 * USDC,
            signing_key_path=guard_keys["signing"],
        )
        chain.send_failure = None
        chain.pending_nonce_value = 1

        out = io.StringIO()
        assert run_execute(executor_config, lain, client=chain, out=out) == 0

        assert chain.distinct_broadcasts() == 2, "yang tertinggal dan yang baru"
        assert count_executions(executor_db) == 2
        for decision_id in (seeded["decision_id"], lain):
            row = fetch_execution(executor_db, decision_id)
            assert row is not None
            assert row["status"] == STATUS_BROADCAST

    def test_a_rejected_execution_exits_nonzero(
        self, executor_config: ExecutorConfig, chain: FakeChain, seeded: dict
    ) -> None:
        run(executor_config, chain, seeded["decision_id"])
        out = io.StringIO()
        assert run_execute(executor_config, seeded["decision_id"], client=chain, out=out) == 1

    def test_nonces_do_not_collide_between_two_executions(
        self,
        executor_config: ExecutorConfig,
        executor_db: str,
        guard_keys: dict[str, Path],
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Jalur biasa: node mengikuti, dan kedua sumber nonce sepakat."""
        lain = seed_approved_decision(
            executor_db,
            agent_id=AGENT_ID,
            mandate_hash=seeded["mandate_hash"],
            recipient=RECIPIENT_B,
            amount_base=2 * USDC,
            signing_key_path=guard_keys["signing"],
        )
        run(executor_config, chain, seeded["decision_id"])
        chain.pending_nonce_value = 1
        run(executor_config, chain, lain)

        assert count_executions(executor_db) == 2
        nonces = sorted(
            fetch_execution(executor_db, d)["nonce"]  # type: ignore[index]
            for d in (seeded["decision_id"], lain)
        )
        assert nonces == [0, 1]

    def test_a_lagging_rpc_cannot_cause_a_repeated_nonce(
        self,
        executor_config: ExecutorConfig,
        executor_db: str,
        guard_keys: dict[str, Path],
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Keputusan operator M6.0: nonce tidak boleh hanya dari RPC.

        Node di tes ini **tidak pernah** melihat transaksi pertama:
        ``pending_nonce`` tetap 0 sepanjang tes. Node yang baru di-restart, atau
        node di belakang penyeimbang beban, berperilaku begini.

        Sampai M5 keadaan ini menghasilkan dua transaksi dengan nonce 0, dan
        yang menangkapnya hanyalah ``UNIQUE(from_addr, nonce)`` — setelah
        transaksi kedua sudah ditandatangani, dengan pengecualian database yang
        sampai ke operator. Sekarang lantai nonce juga dibaca dari
        ``executions``, yang di-commit sebelum broadcast, jadi eksekusi kedua
        memilih nonce 1 tanpa pernah bertabrakan.
        """
        lain = seed_approved_decision(
            executor_db,
            agent_id=AGENT_ID,
            mandate_hash=seeded["mandate_hash"],
            recipient=RECIPIENT_B,
            amount_base=2 * USDC,
            signing_key_path=guard_keys["signing"],
        )
        run(executor_config, chain, seeded["decision_id"])
        assert chain.pending_nonce_value == 0, "node sengaja dibiarkan tertinggal"

        run(executor_config, chain, lain)

        assert count_executions(executor_db) == 2
        nonces = sorted(
            fetch_execution(executor_db, d)["nonce"]  # type: ignore[index]
            for d in (seeded["decision_id"], lain)
        )
        assert nonces == [0, 1]
        assert chain.distinct_broadcasts() == 2

    def test_the_unique_constraint_is_still_the_last_layer(
        self, executor_config: ExecutorConfig, executor_db: str, chain: FakeChain, seeded: dict
    ) -> None:
        """``UNIQUE(from_addr, nonce)`` tetap ada, dan tetap menolak.

        Lantai nonce di aplikasi mencegah tabrakan yang **kami** sebabkan.
        Constraint ini mencegah sisanya, dan ia tidak boleh hilang hanya karena
        lapis di atasnya jadi lebih baik.
        """
        run(executor_config, chain, seeded["decision_id"])
        row = fetch_execution(executor_db, seeded["decision_id"])
        assert row is not None

        with psycopg.connect(executor_db) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.UniqueViolation):
                cur.execute(
                    """
                    INSERT INTO executions (
                      decision_id, tx_hash, from_addr, to_addr, amount_base,
                      nonce, raw_tx, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'signed')
                    """,
                    (
                        str(uuid.uuid4()),
                        "0x" + "ab" * 32,
                        row["from_addr"],
                        row["to_addr"],
                        1,
                        row["nonce"],
                        b"\x02palsu",
                    ),
                )


# --------------------------------------------------------------------------- #
# Bahan kunci tidak bocor
# --------------------------------------------------------------------------- #


class TestNoKeyMaterialLeaks:
    def test_the_wallet_key_never_reaches_stdout(
        self, executor_config: ExecutorConfig, chain: FakeChain, seeded: dict
    ) -> None:
        out = io.StringIO()
        runtime = open_runtime(executor_config, client=chain)
        start_executor(executor_config, client=chain, out=out)
        outcome = execute_decision(runtime, seeded["decision_id"], out=out)
        chain.confirm(outcome.tx_hash)
        recover_pending(runtime, out=out)

        keluaran = out.getvalue().lower()
        assert TEST_WALLET_SEED[2:].lower() not in keluaran

    def test_the_guard_signing_key_never_reaches_stdout(
        self,
        executor_config: ExecutorConfig,
        chain: FakeChain,
        guard_keys: dict[str, Path],
        seeded: dict,
    ) -> None:
        rahasia = json.loads(guard_keys["signing"].read_text(encoding="utf-8"))["private_key"]
        out = io.StringIO()
        runtime = open_runtime(executor_config, client=chain)
        execute_decision(runtime, seeded["decision_id"], out=out)
        assert rahasia not in out.getvalue()

    def test_the_database_url_never_reaches_stdout(
        self, executor_config: ExecutorConfig, chain: FakeChain, seeded: dict
    ) -> None:
        out = io.StringIO()
        start_executor(executor_config, client=chain, out=out)
        runtime = open_runtime(executor_config, client=chain)
        execute_decision(runtime, seeded["decision_id"], out=out)
        assert "devonly_not_a_secret" not in out.getvalue()

    def test_no_key_material_is_stored_in_the_database(
        self, executor_config: ExecutorConfig, executor_db: str, chain: FakeChain, seeded: dict
    ) -> None:
        """``raw_tx`` memuat tanda tangan, bukan kunci yang membuatnya."""
        run(executor_config, chain, seeded["decision_id"])
        row = fetch_execution(executor_db, seeded["decision_id"])
        assert row is not None
        assert bytes.fromhex(TEST_WALLET_SEED[2:]) not in bytes(row["raw_tx"])


# --------------------------------------------------------------------------- #
# Rumus nonce terhadap baris sungguhan
# --------------------------------------------------------------------------- #


class TestNonceFormulaAgainstRealRows:
    """Rumus M6.0.1 terhadap Postgres, bukan terhadap kursor tiruan.

    Tes unit di ``tests/unit/test_executor.py`` memakai kursor tiruan yang
    **menirukan** ``coalesce(max(nonce), -1)``. Tiruan yang keliru akan lulus
    sendiri, jadi dua kasus pangkal rumus ini dijalankan di sini terhadap
    database yang sesungguhnya: kasus (a) tabel kosong dan kasus (b) satu baris
    dengan nonce 5 sementara node menjawab 5.

    Keduanya memanggil :func:`payment_guard.executor.next_nonce` langsung, di
    luar ``execute_decision``, karena yang diperiksa adalah rumusnya — bukan
    jalur eksekusinya, yang sudah punya tesnya sendiri di atas.
    """

    def nonce_for(self, url: str, *, pending: int) -> int:
        with psycopg.connect(url) as conn, conn.cursor() as cur:
            return next_nonce(cur, FakeChain(pending_nonce_value=pending), TEST_WALLET_ADDRESS)

    def max_nonce_as_postgres_sees_it(self, url: str) -> int:
        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute(SELECT_MAX_NONCE, (TEST_WALLET_ADDRESS,))
            row = cur.fetchone()
            assert row is not None, "agregat harus selalu mengembalikan satu baris"
            return to_int(row[0])

    def test_case_a_an_empty_table_yields_zero(self, executor_db: str) -> None:
        """Tabel kosong, node di 0, hasilnya 0 — nonce pertama yang sah."""
        assert count_executions(executor_db) == 0
        assert self.max_nonce_as_postgres_sees_it(executor_db) == NO_NONCE_USED
        assert self.nonce_for(executor_db, pending=0) == 0

    def test_case_b_a_row_at_five_with_the_node_at_five_yields_six(
        self,
        executor_config: ExecutorConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Satu baris sungguhan bernonce 5, node menjawab 5, hasilnya 6.

        Di sinilah dua satuan itu bertemu: 5 di ``executions`` adalah nonce
        yang **sudah** dipakai, 5 dari node adalah nonce yang **akan** dipakai.
        Mengambil yang terbesar tanpa menyamakan satuannya lebih dulu akan
        memberi 5, dan ``UNIQUE(from_addr, nonce)`` yang menolaknya — setelah
        transaksi kedua ditandatangani.
        """
        run(executor_config, chain, seeded["decision_id"])
        with psycopg.connect(executor_db) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE executions SET nonce = %s WHERE decision_id = %s",
                (5, seeded["decision_id"]),
            )
            conn.commit()

        assert self.max_nonce_as_postgres_sees_it(executor_db) == 5
        assert self.nonce_for(executor_db, pending=5) == 6

    def test_the_node_still_wins_when_it_is_further_ahead(
        self,
        executor_config: ExecutorConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Kasus (c) dengan baris sungguhan: transaksi dari luar sistem ini.

        Hanya node yang melihatnya, jadi hanya node yang bisa melaporkannya.
        """
        run(executor_config, chain, seeded["decision_id"])
        with psycopg.connect(executor_db) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE executions SET nonce = %s WHERE decision_id = %s",
                (5, seeded["decision_id"]),
            )
            conn.commit()

        assert self.nonce_for(executor_db, pending=8) == 8

    def test_a_failed_row_still_holds_its_nonce(
        self,
        executor_config: ExecutorConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """``coalesce(max(nonce), -1)`` tidak memfilter status, dan itu sengaja.

        Transaksi yang gagal bisa masih beredar di mempool. Memakai ulang
        nonce-nya berarti dua transaksi berbeda bersaing untuk slot yang sama.
        """
        run(executor_config, chain, seeded["decision_id"])
        with psycopg.connect(executor_db) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE executions SET nonce = %s, status = %s WHERE decision_id = %s",
                (5, STATUS_FAILED, seeded["decision_id"]),
            )
            conn.commit()

        assert self.nonce_for(executor_db, pending=0) == 6
