"""Watcher dan Reconciler terhadap Postgres sungguhan.

PRD v2 bagian 10 dan 11, Arsitektur v1.1 bagian 10 dan 11.

Yang nyata di berkas ini adalah databasenya, GRANT-nya, dan constraint-nya.
Yang ditiru hanya chain, lewat :class:`fake_chain.FakeChain`. Tidak ada
jaringan yang tersentuh dan tidak ada transaksi yang disiarkan ke chain
sungguhan.

Satu ``FakeChain`` dibagi Executor dan Watcher di tes-tes di bawah. Itu bukan
kemalasan: di produksi memang ada **satu** chain, dan Watcher melihat
transaksi yang dikirim Executor justru lewat chain itu. Tiruan terpisah per
proses akan menyembunyikan setiap kesalahan di tempat keduanya bertemu.
"""

from __future__ import annotations

import dataclasses
import io
import re
from pathlib import Path
from typing import Any

import psycopg
import pytest
from conftest_watcher import (
    TEST_CONFIRMATIONS,
    cursor_value,
    reconciliation_rows,
    set_cursor,
    settlement_rows,
    table_snapshot,
)
from psycopg import sql

from conftest_executor import (
    CHAIN_ID,
    USDC_ADDRESS,
    audit_events,
    audit_payloads_for,
    count_executions,
    seed_approved_decision,
    seed_mandate,
)
from conftest_guard import RECIPIENT_A, RECIPIENT_B, USDC
from fake_chain import TEST_WALLET_ADDRESS, TEST_WALLET_SEED, FakeChain
from payment_guard.chain import ChainError
from payment_guard.config import ExecutorConfig, ReconcilerConfig, WatcherConfig
from payment_guard.events import Event
from payment_guard.executor import execute_decision, open_runtime
from payment_guard.reconciler import (
    STATE_EXECUTED_NOT_SETTLED,
    STATE_EXPIRED,
    STATE_INVALIDATED,
    STATE_MATCHED,
    STATE_MISMATCH,
    STATE_UNMATCHED_ONCHAIN,
    SUBJECT_DECISION,
    SUBJECT_EXECUTION,
    SUBJECT_SETTLEMENT,
    reconcile_once,
    report_once,
    run_report,
)
from payment_guard.watcher import (
    STATUS_FINAL,
    STATUS_PENDING,
    STATUS_REORGED,
    WatcherError,
    close_runtime,
    run_watch,
    scan_once,
    start_watcher,
)
from payment_guard.watcher import (
    open_runtime as open_watcher,
)

pytestmark = pytest.mark.integration

AGENT_ID = "demo-agent"
AMOUNT = 10 * USDC

#: Blok tempat transfer uji ditaruh. Angkanya kecil supaya kedalaman
#: konfirmasi bisa dicapai tanpa membuat ribuan blok tiruan.
BLOCK = 10

#: Hash transaksi untuk transfer yang tidak punya execution sama sekali.
FOREIGN_TX = "0x" + "fe" * 32


@pytest.fixture
def chain() -> FakeChain:
    chain = FakeChain()
    chain.advance_to(BLOCK)
    return chain


@pytest.fixture
def seeded(executor_db: str, guard_keys: dict[str, Path]) -> dict[str, Any]:
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


def broadcast(config: ExecutorConfig, chain: FakeChain, decision_id: str) -> str:
    """Jalankan Executor sampai transaksinya tersiar; kembalikan ``tx_hash``."""
    out = io.StringIO()
    execute_decision(open_runtime(config, client=chain), decision_id, out=out)
    assert chain.sent, "tidak ada yang disiarkan"
    from fake_chain import tx_hash_of

    return tx_hash_of(chain.sent[-1])


def watch(
    config: WatcherConfig,
    chain: FakeChain,
    *,
    cycles: int = 1,
    from_block: int | None = 0,
    out: io.StringIO | None = None,
) -> list[Any]:
    """Jalankan ``cycles`` siklus Watcher pada runtime yang sama.

    ``from_block`` default **0**, dan nolnya eksplisit. Sejak M6.1 Watcher
    menolak start tanpa kursor dan tanpa ``--from-block``; nol bukan nilai
    terlarang, yang terlarang adalah nol yang tersirat. Rantai tiruan di berkas
    ini pendek, jadi 0 adalah blok awal yang benar untuknya.

    ``from_block`` hanya diberikan pada siklus pertama, sama seperti
    :func:`payment_guard.watcher.run_watch`.
    """
    runtime = open_watcher(config, client=chain)
    try:
        return [
            scan_once(runtime, from_block=from_block if index == 0 else None, out=out)
            for index in range(cycles)
        ]
    finally:
        close_runtime(runtime)


def deep_enough(chain: FakeChain, block: int = BLOCK) -> None:
    """Majukan kepala rantai sampai ``block`` melewati kedalaman konfirmasi."""
    chain.advance_to(block + TEST_CONFIRMATIONS)


# --------------------------------------------------------------------------- #
# Jalur bahagia: satu transfer cocok, satu settlement
# --------------------------------------------------------------------------- #


class TestAMatchingTransfer:
    def test_one_transfer_becomes_one_settlement(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        tx_hash = broadcast(executor_config, chain, seeded["decision_id"])
        chain.add_transfer(
            tx_hash=tx_hash, to_addr=RECIPIENT_A, amount_base=AMOUNT, block_number=BLOCK
        )
        deep_enough(chain)

        watch(watcher_config, chain)

        rows = settlement_rows(executor_db)
        assert len(rows) == 1
        row = rows[0]
        assert row["tx_hash"] == tx_hash
        assert row["to_addr"] == RECIPIENT_A
        assert int(row["amount_base"]) == AMOUNT
        assert row["from_addr"] == TEST_WALLET_ADDRESS
        assert row["chain_id"] == CHAIN_ID

    def test_it_reaches_final_and_says_so_in_the_audit_log(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        tx_hash = broadcast(executor_config, chain, seeded["decision_id"])
        chain.add_transfer(
            tx_hash=tx_hash, to_addr=RECIPIENT_A, amount_base=AMOUNT, block_number=BLOCK
        )
        deep_enough(chain)

        # Dua siklus: yang pertama menulis barisnya `pending`, yang kedua
        # memeriksanya ulang dan memfinalkan. Itu memang urutannya — baris
        # hanya menjadi final setelah diperiksa ulang, bukan saat ditulis.
        watch(watcher_config, chain, cycles=2)

        rows = settlement_rows(executor_db)
        assert rows[0]["status"] == STATUS_FINAL
        assert rows[0]["finalized_at"] is not None
        assert ("watcher", "settlement_finalized") in audit_events(executor_db)

    def test_the_reconciler_calls_it_matched(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        reconciler_config: ReconcilerConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        tx_hash = broadcast(executor_config, chain, seeded["decision_id"])
        chain.add_transfer(
            tx_hash=tx_hash, to_addr=RECIPIENT_A, amount_base=AMOUNT, block_number=BLOCK
        )
        deep_enough(chain)
        watch(watcher_config, chain, cycles=2)

        report = reconcile_once(reconciler_config)

        assert report.alarms == []
        rows = reconciliation_rows(executor_db)
        assert len(rows) == 1
        assert rows[0]["state"] == STATE_MATCHED
        assert rows[0]["subject_type"] == SUBJECT_SETTLEMENT
        assert rows[0]["execution_id"] is not None
        assert ("reconciler", "reconciliation_state_changed") in audit_events(executor_db)

    def test_a_second_reconcile_writes_nothing_new(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        reconciler_config: ReconcilerConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Audit hanya ditulis ketika state **berubah**.

        ``guard reconcile`` dijalankan berulang. ``audit_log`` yang dipenuhi
        "masih cocok" setiap lima menit berhenti bisa dibaca sebagai bukti.
        """
        tx_hash = broadcast(executor_config, chain, seeded["decision_id"])
        chain.add_transfer(
            tx_hash=tx_hash, to_addr=RECIPIENT_A, amount_base=AMOUNT, block_number=BLOCK
        )
        deep_enough(chain)
        watch(watcher_config, chain, cycles=2)

        first = reconcile_once(reconciler_config)
        before = len(audit_events(executor_db))
        second = reconcile_once(reconciler_config)

        assert first.changed == 1
        assert second.changed == 0
        assert second.findings[0].state == STATE_MATCHED
        assert len(audit_events(executor_db)) == before


# --------------------------------------------------------------------------- #
# Transfer yang tidak cocok
# --------------------------------------------------------------------------- #


class TestATransferThatDoesNotMatch:
    """PRD v2 bagian 11 nomor 2: beda ``to_addr`` atau ``amount_base`` -> mismatch."""

    def settle_with(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        chain: FakeChain,
        decision_id: str,
        *,
        to_addr: str,
        amount_base: int,
    ) -> str:
        tx_hash = broadcast(executor_config, chain, decision_id)
        chain.add_transfer(
            tx_hash=tx_hash, to_addr=to_addr, amount_base=amount_base, block_number=BLOCK
        )
        deep_enough(chain)
        watch(watcher_config, chain, cycles=2)
        return tx_hash

    def test_a_different_amount_is_a_mismatch(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        reconciler_config: ReconcilerConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        self.settle_with(
            executor_config,
            watcher_config,
            chain,
            seeded["decision_id"],
            to_addr=RECIPIENT_A,
            amount_base=AMOUNT + 1,
        )

        report = reconcile_once(reconciler_config)

        assert [f.state for f in report.alarms] == [STATE_MISMATCH]
        row = reconciliation_rows(executor_db)[0]
        assert row["state"] == STATE_MISMATCH
        assert row["detail"]["differs"] == ["amount_base"]
        # Jumlah sebagai string: base units bisa melampaui 2^53, dan payload
        # audit tidak pernah melewatkan angka lewat float.
        assert row["detail"]["expected_amount_base"] == str(AMOUNT)
        assert row["detail"]["settled_amount_base"] == str(AMOUNT + 1)

    def test_a_different_recipient_is_a_mismatch(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        reconciler_config: ReconcilerConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        self.settle_with(
            executor_config,
            watcher_config,
            chain,
            seeded["decision_id"],
            to_addr=RECIPIENT_B,
            amount_base=AMOUNT,
        )

        report = reconcile_once(reconciler_config)

        assert [f.state for f in report.alarms] == [STATE_MISMATCH]
        row = reconciliation_rows(executor_db)[0]
        assert row["detail"]["differs"] == ["to_addr"]
        assert row["detail"]["expected_to_addr"] == RECIPIENT_A

    def test_both_differing_are_both_reported(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        reconciler_config: ReconcilerConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        self.settle_with(
            executor_config,
            watcher_config,
            chain,
            seeded["decision_id"],
            to_addr=RECIPIENT_B,
            amount_base=AMOUNT * 2,
        )

        reconcile_once(reconciler_config)

        row = reconciliation_rows(executor_db)[0]
        assert row["detail"]["differs"] == ["to_addr", "amount_base"]

    def test_a_transfer_with_no_execution_is_unmatched_onchain(
        self,
        watcher_config: WatcherConfig,
        reconciler_config: ReconcilerConfig,
        executor_db: str,
        chain: FakeChain,
    ) -> None:
        """Uang bergerak dari wallet kami lewat jalur di luar Payment Guard.

        Ini temuan yang paling penting di seluruh M6, dan satu-satunya alasan
        Watcher menyaring per **pengirim** dan bukan per penerima: menyaring
        per penerima akan membuat transfer semacam ini tidak pernah terlihat.
        """
        chain.add_transfer(
            tx_hash=FOREIGN_TX, to_addr=RECIPIENT_B, amount_base=5 * USDC, block_number=BLOCK
        )
        deep_enough(chain)
        watch(watcher_config, chain, cycles=2)

        report = reconcile_once(reconciler_config)

        assert [f.state for f in report.alarms] == [STATE_UNMATCHED_ONCHAIN]
        row = reconciliation_rows(executor_db)[0]
        assert row["state"] == STATE_UNMATCHED_ONCHAIN
        assert row["execution_id"] is None
        assert row["detail"]["tx_hash"] == FOREIGN_TX

    def test_it_is_not_ignored_silently_even_before_the_reconciler_runs(
        self,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
    ) -> None:
        """Baris ``settlements``-nya ada sejak Watcher melihatnya.

        Watcher tidak punya SELECT pada ``executions``, jadi ia tidak bisa
        tahu apakah transfer ini punya padanan — dan karena itu ia mencatat
        **semuanya**. Yang menamainya adalah Reconciler.
        """
        chain.add_transfer(
            tx_hash=FOREIGN_TX, to_addr=RECIPIENT_B, amount_base=5 * USDC, block_number=BLOCK
        )
        deep_enough(chain)
        watch(watcher_config, chain)

        rows = settlement_rows(executor_db)
        assert len(rows) == 1
        assert rows[0]["tx_hash"] == FOREIGN_TX


# --------------------------------------------------------------------------- #
# Idempotensi
# --------------------------------------------------------------------------- #


class TestProcessingTheSameBlockTwice:
    """PRD v2 bagian 10 nomor 2: ``ON CONFLICT ... DO NOTHING``."""

    def prepared(self, executor_config: ExecutorConfig, chain: FakeChain, decision_id: str) -> str:
        tx_hash = broadcast(executor_config, chain, decision_id)
        chain.add_transfer(
            tx_hash=tx_hash, to_addr=RECIPIENT_A, amount_base=AMOUNT, block_number=BLOCK
        )
        deep_enough(chain)
        return tx_hash

    def test_a_rewound_cursor_does_not_duplicate_the_settlement(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        watcher_url: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        self.prepared(executor_config, chain, seeded["decision_id"])
        watch(watcher_config, chain)
        assert len(settlement_rows(executor_db)) == 1

        # Mundurkan kursor dengan sengaja dan pindai rentang yang sama lagi.
        set_cursor(executor_db, BLOCK - 1)
        watch(watcher_config, chain)

        assert len(settlement_rows(executor_db)) == 1

    def test_many_cycles_leave_exactly_one_row(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        self.prepared(executor_config, chain, seeded["decision_id"])
        for _ in range(4):
            set_cursor(executor_db, BLOCK - 1)
            watch(watcher_config, chain)
        assert len(settlement_rows(executor_db)) == 1

    def test_a_rescan_does_not_pull_a_final_row_back_to_pending(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """``DO NOTHING`` mengabaikan nilai yang dimasukkan seluruhnya.

        Kalau ia ``DO UPDATE``, memindai ulang blok yang sama akan menulis
        ``pending`` di atas baris yang sudah ``final`` — dan rekonsiliasi
        hanya menghitung yang ``final``.
        """
        self.prepared(executor_config, chain, seeded["decision_id"])
        watch(watcher_config, chain, cycles=2)
        assert settlement_rows(executor_db)[0]["status"] == STATUS_FINAL

        set_cursor(executor_db, BLOCK - 1)
        watch(watcher_config, chain)

        assert settlement_rows(executor_db)[0]["status"] == STATUS_FINAL

    def test_the_unique_constraint_is_still_the_last_layer(
        self, executor_db: str, chain: FakeChain
    ) -> None:
        """Yang menolak baris ganda adalah database, bukan Python."""
        with psycopg.connect(executor_db) as conn, conn.cursor() as cur:
            values = (
                CHAIN_ID,
                FOREIGN_TX,
                0,
                BLOCK,
                "0x" + "11" * 32,
                TEST_WALLET_ADDRESS,
                RECIPIENT_A,
                str(AMOUNT),
                STATUS_PENDING,
            )
            sql = (
                "INSERT INTO settlements (chain_id, tx_hash, log_index, block_number, "
                "block_hash, from_addr, to_addr, amount_base, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            )
            cur.execute(sql, values)
            conn.commit()
            with pytest.raises(psycopg.errors.UniqueViolation):
                cur.execute(sql, values)
            conn.rollback()


# --------------------------------------------------------------------------- #
# Kedalaman konfirmasi
# --------------------------------------------------------------------------- #


class TestConfirmationDepth:
    def seen_at_head(
        self,
        executor_config: ExecutorConfig,
        chain: FakeChain,
        decision_id: str,
        *,
        head: int,
    ) -> str:
        tx_hash = broadcast(executor_config, chain, decision_id)
        chain.add_transfer(
            tx_hash=tx_hash, to_addr=RECIPIENT_A, amount_base=AMOUNT, block_number=BLOCK
        )
        chain.advance_to(head)
        return tx_hash

    def test_below_the_depth_nothing_is_scanned_at_all(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Blok yang belum aman tidak dipindai, jadi barisnya belum ada.

        Itu bukan kehilangan: kursor juga tidak maju melewatinya, jadi blok
        yang sama akan dipindai begitu ia cukup dalam.
        """
        self.seen_at_head(
            executor_config, chain, seeded["decision_id"], head=BLOCK + TEST_CONFIRMATIONS - 1
        )
        reports = watch(watcher_config, chain)

        assert settlement_rows(executor_db) == []
        assert reports[0].last_block < BLOCK

    def test_one_block_short_is_not_final(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Batas inklusifnya diuji, bukan hanya jauh di dalam atau jauh di luar.

        Kursor diset mundur supaya bloknya ikut dipindai walau belum aman,
        sehingga yang diuji benar-benar transisi ``pending -> final`` dan
        bukan keputusan memindai.
        """
        self.seen_at_head(executor_config, chain, seeded["decision_id"], head=BLOCK)
        set_cursor(executor_db, BLOCK - 1)
        # Kepala rantai satu blok kurang dari yang dibutuhkan.
        chain.advance_to(BLOCK + TEST_CONFIRMATIONS - 1)
        with psycopg.connect(executor_db) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO settlements (chain_id, tx_hash, log_index, block_number, "
                "block_hash, from_addr, to_addr, amount_base, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    CHAIN_ID,
                    FOREIGN_TX,
                    0,
                    BLOCK,
                    chain.block_hash(BLOCK),
                    TEST_WALLET_ADDRESS,
                    RECIPIENT_A,
                    str(AMOUNT),
                    STATUS_PENDING,
                ),
            )
            conn.commit()

        watch(watcher_config, chain)

        row = next(r for r in settlement_rows(executor_db) if r["tx_hash"] == FOREIGN_TX)
        assert row["status"] == STATUS_PENDING
        assert row["finalized_at"] is None
        # Tetap diperiksa: operator bisa membedakan "belum dalam" dari
        # "tidak pernah dilihat lagi".
        assert row["last_verified_at"] is not None

    def test_exactly_at_the_depth_it_becomes_final(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        self.seen_at_head(
            executor_config, chain, seeded["decision_id"], head=BLOCK + TEST_CONFIRMATIONS
        )
        watch(watcher_config, chain, cycles=2)
        assert settlement_rows(executor_db)[0]["status"] == STATUS_FINAL

    def test_a_pending_row_is_not_reconciled(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        reconciler_config: ReconcilerConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Hanya ``final`` yang dihitung rekonsiliasi (PRD v2 bagian 10).

        Satu siklus saja: barisnya sudah ditulis tapi belum diperiksa ulang,
        jadi ia masih ``pending``. Jendela itu nyata — selebar satu jeda
        ``WATCHER_INTERVAL_SECONDS`` di produksi.
        """
        self.seen_at_head(
            executor_config, chain, seeded["decision_id"], head=BLOCK + TEST_CONFIRMATIONS
        )
        watch(watcher_config, chain, cycles=1)
        assert settlement_rows(executor_db)[0]["status"] == STATUS_PENDING

        report = reconcile_once(reconciler_config)

        assert [f.subject_type for f in report.findings] != [SUBJECT_SETTLEMENT]
        assert reconciliation_rows(executor_db) == [] or all(
            r["subject_type"] != SUBJECT_SETTLEMENT for r in reconciliation_rows(executor_db)
        )

    def test_the_depth_comes_from_config_not_from_code(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Kedalaman yang lebih besar menahan baris yang sama tetap ``pending``."""
        self.seen_at_head(
            executor_config, chain, seeded["decision_id"], head=BLOCK + TEST_CONFIRMATIONS
        )
        dalam = dataclasses.replace(watcher_config, confirmations=TEST_CONFIRMATIONS + 5)
        set_cursor(executor_db, BLOCK - 1)
        watch(dalam, chain, cycles=2)

        rows = settlement_rows(executor_db)
        assert rows == [] or rows[0]["status"] == STATUS_PENDING


# --------------------------------------------------------------------------- #
# Reorg
# --------------------------------------------------------------------------- #


class TestReorg:
    """State machine PRD v2 bagian 10, semuanya dengan RPC tiruan.

    Catatan tentang ``pending``: Watcher hanya memindai blok yang sudah
    melewati kedalaman konfirmasi, jadi setiap baris yang baru ditulis sudah
    memenuhi syarat ``final`` pada pemeriksaan **berikutnya**. ``pending``
    karena itu bertahan tepat satu siklus, dan tes di bawah yang butuh baris
    ``pending`` menjalankan satu siklus lalu mengubah rantai sebelum siklus
    kedua. Itu bukan keadaan yang dikarang: di produksi jendela itu persis
    selebar satu jeda ``WATCHER_INTERVAL_SECONDS``.
    """

    def pending(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        chain: FakeChain,
        decision_id: str,
    ) -> str:
        """Satu baris ``pending`` yang nyata: dipindai, belum diperiksa ulang."""
        tx_hash = broadcast(executor_config, chain, decision_id)
        chain.add_transfer(
            tx_hash=tx_hash, to_addr=RECIPIENT_A, amount_base=AMOUNT, block_number=BLOCK
        )
        deep_enough(chain)
        watch(watcher_config, chain, cycles=1)
        return tx_hash

    def finalized(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        chain: FakeChain,
        decision_id: str,
    ) -> str:
        tx_hash = broadcast(executor_config, chain, decision_id)
        chain.add_transfer(
            tx_hash=tx_hash, to_addr=RECIPIENT_A, amount_base=AMOUNT, block_number=BLOCK
        )
        deep_enough(chain)
        watch(watcher_config, chain, cycles=2)
        return tx_hash

    def test_a_vanished_transaction_becomes_reorged(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Hash berubah **dan** receipt hilang (PRD v2 bagian 10)."""
        tx_hash = self.pending(executor_config, watcher_config, chain, seeded["decision_id"])
        assert settlement_rows(executor_db)[0]["status"] == STATUS_PENDING

        chain.reorg_block(BLOCK)
        chain.drop_transaction(tx_hash)
        watch(watcher_config, chain)

        rows = settlement_rows(executor_db)
        assert rows[0]["status"] == STATUS_REORGED
        assert ("watcher", "settlement_reorged") in audit_events(executor_db)

    def test_the_row_is_never_deleted(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Yang pernah terlihat di chain tetap tercatat pernah terlihat."""
        tx_hash = self.pending(executor_config, watcher_config, chain, seeded["decision_id"])
        chain.reorg_block(BLOCK)
        chain.drop_transaction(tx_hash)
        watch(watcher_config, chain)

        assert len(settlement_rows(executor_db)) == 1

    def test_a_moved_transaction_keeps_one_row_and_changes_its_block(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """``pending -> pending`` (dipindah): kunci uniknya tidak berubah."""
        tx_hash = self.pending(executor_config, watcher_config, chain, seeded["decision_id"])

        chain.reorg_block(BLOCK)
        chain.move_transaction(tx_hash, to_block=BLOCK + 1)
        watch(watcher_config, chain)

        rows = settlement_rows(executor_db)
        assert len(rows) == 1
        assert rows[0]["status"] == STATUS_PENDING
        assert rows[0]["block_number"] == BLOCK + 1
        assert ("watcher", "settlement_moved") in audit_events(executor_db)

    def test_the_cursor_rewinds_past_the_shaken_block(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Blok yang hash-nya berubah harus dipindai ulang.

        Log di blok pengganti bisa berbeda, dan log baru di sana tidak akan
        pernah terlihat kalau kursor sudah melewatinya.
        """
        tx_hash = broadcast(executor_config, chain, seeded["decision_id"])
        chain.add_transfer(
            tx_hash=tx_hash, to_addr=RECIPIENT_A, amount_base=AMOUNT, block_number=BLOCK
        )
        deep_enough(chain)
        watch(watcher_config, chain)
        maju = cursor_value(executor_db)
        assert maju is not None and maju >= BLOCK

        chain.reorg_block(BLOCK)
        chain.drop_transaction(tx_hash)
        reports = watch(watcher_config, chain)

        assert reports[0].cursor_rewound_to == BLOCK - 1
        # Kursor sudah maju lagi di siklus yang sama, karena pemeriksaan
        # berjalan sebelum pemindaian.
        setelah = cursor_value(executor_db)
        assert setelah is not None and setelah >= BLOCK - 1

    def test_a_restored_transaction_comes_back_to_pending(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """``reorged -> pending``: jalan kembali selalu ada karena barisnya ada."""
        tx_hash = self.pending(executor_config, watcher_config, chain, seeded["decision_id"])
        chain.reorg_block(BLOCK)
        chain.drop_transaction(tx_hash)
        watch(watcher_config, chain)
        assert settlement_rows(executor_db)[0]["status"] == STATUS_REORGED

        chain.move_transaction(tx_hash, to_block=BLOCK + 2)
        watch(watcher_config, chain)

        rows = settlement_rows(executor_db)
        assert len(rows) == 1
        assert rows[0]["status"] == STATUS_PENDING
        assert rows[0]["block_number"] == BLOCK + 2
        assert ("watcher", "settlement_restored") in audit_events(executor_db)

    def test_a_final_row_that_reorgs_raises_an_alarm(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """``final -> reorged``: janji ke rekonsiliasi yang dibatalkan."""
        self.finalized(executor_config, watcher_config, chain, seeded["decision_id"])
        assert settlement_rows(executor_db)[0]["status"] == STATUS_FINAL

        chain.reorg_block(BLOCK)
        reports = watch(watcher_config, chain)

        assert reports[0].alarms == 1
        row = settlement_rows(executor_db)[0]
        assert row["status"] == STATUS_REORGED
        # `finalized_at` dibiarkan terisi: ia sejarah, dan ia yang membedakan
        # baris ini dari baris yang hanya pernah `pending`.
        assert row["finalized_at"] is not None
        assert ("watcher", "alarm_final_settlement_reorged") in audit_events(executor_db)

    def test_the_reconciler_invalidates_it(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        reconciler_config: ReconcilerConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        self.finalized(executor_config, watcher_config, chain, seeded["decision_id"])
        reconcile_once(reconciler_config)
        assert reconciliation_rows(executor_db)[0]["state"] == STATE_MATCHED

        chain.reorg_block(BLOCK)
        watch(watcher_config, chain)
        report = reconcile_once(reconciler_config)

        assert [f.state for f in report.alarms] == [STATE_INVALIDATED]
        row = reconciliation_rows(executor_db)[0]
        assert row["state"] == STATE_INVALIDATED
        assert row["settlement_id"] is not None

    def test_a_row_that_was_never_final_is_not_invalidated(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        reconciler_config: ReconcilerConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Ia belum pernah dihitung sebagai pembayaran, jadi tidak ada yang dibatalkan.

        Menamainya ``invalidated`` akan menyamakannya dengan kejadian yang
        jauh lebih serius.
        """
        tx_hash = self.pending(executor_config, watcher_config, chain, seeded["decision_id"])
        chain.reorg_block(BLOCK)
        chain.drop_transaction(tx_hash)
        watch(watcher_config, chain)
        assert settlement_rows(executor_db)[0]["status"] == STATUS_REORGED
        assert settlement_rows(executor_db)[0]["finalized_at"] is None

        report = reconcile_once(reconciler_config)

        assert [f.state for f in report.findings if f.subject_type == SUBJECT_SETTLEMENT] == []

    def test_rescanning_after_a_reorg_reaches_a_consistent_result(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        reconciler_config: ReconcilerConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Hasil akhirnya satu baris, cocok, dan tanpa alarm yang menggantung.

        Reorg yang memindahkan transaksi ke blok lain harus berakhir sama
        dengan reorg yang tidak pernah terjadi: ini yang dimaksud "hasil
        konsisten".
        """
        tx_hash = self.pending(executor_config, watcher_config, chain, seeded["decision_id"])

        chain.reorg_block(BLOCK)
        chain.move_transaction(tx_hash, to_block=BLOCK + 1)
        chain.advance_to(BLOCK + 1 + TEST_CONFIRMATIONS)
        watch(watcher_config, chain, cycles=3)

        rows = settlement_rows(executor_db)
        assert len(rows) == 1
        assert rows[0]["status"] == STATUS_FINAL
        assert rows[0]["block_number"] == BLOCK + 1
        report = reconcile_once(reconciler_config)
        assert report.alarms == []
        assert reconciliation_rows(executor_db)[0]["state"] == STATE_MATCHED


# --------------------------------------------------------------------------- #
# Sisi sebaliknya: sudah dikirim, chain tidak menunjukkannya
# --------------------------------------------------------------------------- #


class TestExecutedButNotSettled:
    def test_past_the_deadline_it_is_recorded(
        self,
        executor_config: ExecutorConfig,
        reconciler_config: ReconcilerConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Batas waktunya nol detik: setiap baris yang sudah disiarkan lewat."""
        broadcast(executor_config, chain, seeded["decision_id"])
        lewat = dataclasses.replace(reconciler_config, deadline_seconds=60)
        # Geser waktu siar ke masa lalu lewat jam database, bukan jam proses.
        with psycopg.connect(executor_db) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE executions SET broadcast_at = now() - make_interval(secs => %s)",
                (3600,),
            )
            conn.commit()

        report = reconcile_once(lewat)

        assert [f.state for f in report.alarms] == [STATE_EXECUTED_NOT_SETTLED]
        row = reconciliation_rows(executor_db)[0]
        assert row["subject_type"] == SUBJECT_EXECUTION
        assert row["state"] == STATE_EXECUTED_NOT_SETTLED
        assert row["execution_id"] is not None
        assert row["decision_id"] == seeded["decision_id"]

    def test_before_the_deadline_it_is_not(
        self,
        executor_config: ExecutorConfig,
        reconciler_config: ReconcilerConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Batas waktu yang terlalu pendek berarti alarm palsu tiap chain melambat."""
        broadcast(executor_config, chain, seeded["decision_id"])

        report = reconcile_once(reconciler_config)

        assert report.alarms == []
        assert reconciliation_rows(executor_db) == []

    def test_a_settled_execution_is_not_flagged(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        reconciler_config: ReconcilerConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        tx_hash = broadcast(executor_config, chain, seeded["decision_id"])
        chain.add_transfer(
            tx_hash=tx_hash, to_addr=RECIPIENT_A, amount_base=AMOUNT, block_number=BLOCK
        )
        deep_enough(chain)
        watch(watcher_config, chain, cycles=2)
        with psycopg.connect(executor_db) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE executions SET broadcast_at = now() - make_interval(secs => %s)",
                (3600,),
            )
            conn.commit()

        report = reconcile_once(dataclasses.replace(reconciler_config, deadline_seconds=60))

        assert [f.state for f in report.findings] == [STATE_MATCHED]

    def test_an_approved_decision_that_expired_unused_is_recorded(
        self,
        reconciler_config: ReconcilerConfig,
        executor_db: str,
        seeded: dict,
    ) -> None:
        """Bukan alarm: jatah yang dilepas karena kedaluwarsa adalah jalur normal.

        Seluruh garis waktu barisnya digeser ke masa lalu, bukan hanya
        ``valid_until``. ``0001_init.sql`` punya
        ``valid_until_after_approval`` dan ``decided_not_before_created``, dan
        memundurkan satu kolom saja akan melanggarnya — yang justru bukti
        constraint-nya bekerja.
        """
        with psycopg.connect(executor_db) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE decisions SET created_at = now() - make_interval(secs => %s), "
                "decided_at = now() - make_interval(secs => %s), "
                "approved_at = now() - make_interval(secs => %s), "
                "valid_until = now() - make_interval(secs => %s) WHERE id = %s",
                (10_800, 7200, 7200, 3600, seeded["decision_id"]),
            )
            conn.commit()

        report = reconcile_once(reconciler_config)

        assert [f.state for f in report.findings] == [STATE_EXPIRED]
        assert report.alarms == []
        row = reconciliation_rows(executor_db)[0]
        assert row["subject_type"] == SUBJECT_DECISION
        assert row["subject_id"] == seeded["decision_id"]


# --------------------------------------------------------------------------- #
# Batas kewenangan
# --------------------------------------------------------------------------- #


class TestTheWatcherChangesNothingElse:
    """Tes eksplisit: Watcher tidak pernah mengubah ``decisions`` atau ``executions``.

    Dua lapis. Lapis pertama adalah GRANT di ``0002_roles.sql``, yang akan
    menolak UPDATE dari role Watcher. Lapis kedua adalah perbandingan isi
    tabel sebelum dan sesudah — yang juga menangkap perubahan yang terjadi
    lewat jalur lain, misalnya kalau suatu hari Watcher dijalankan dengan URL
    yang salah.
    """

    def test_decisions_and_executions_are_byte_identical_after_a_scan(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        tx_hash = broadcast(executor_config, chain, seeded["decision_id"])
        chain.add_transfer(
            tx_hash=tx_hash, to_addr=RECIPIENT_A, amount_base=AMOUNT, block_number=BLOCK
        )
        deep_enough(chain)
        before = {
            name: table_snapshot(executor_db, name)
            for name in ("decisions", "executions", "mandates", "active_mandates")
        }

        watch(watcher_config, chain, cycles=3)

        after = {
            name: table_snapshot(executor_db, name)
            for name in ("decisions", "executions", "mandates", "active_mandates")
        }
        assert after == before

    def test_the_watcher_role_cannot_read_what_matters_in_executions(
        self, watcher_url: str, executor_db: str, chain: FakeChain, seeded: dict
    ) -> None:
        """Bukan hanya tidak menulis: isi barisnya tidak boleh dibaca.

        Di M6 tes ini berbunyi "tidak boleh membaca `executions` sama sekali".
        M6.2 membuka satu kolom, `from_addr`, lewat migrasi 0003 — jadi yang
        dijaga sekarang adalah kolom yang menentukan: `decision_id`,
        `amount_base`, dan `raw_tx`.

        Pencocokan tetap pekerjaan Reconciler. Proses yang bisa membaca
        keputusan **dan** menulis `settlements` sekaligus adalah proses yang
        bisa, suatu hari, "memperbaiki" salah satunya — dan satu kolom alamat
        tidak memindahkan kemampuan itu.

        Cakupan penuh per kolom ada di
        ``TestTheWatcherReadsOnlyFromAddr``; yang di sini adalah tiga kolom
        yang paling penting, di tempat yang membaca tentang kewenangan Watcher.
        """
        with psycopg.connect(watcher_url) as conn, conn.cursor() as cur:
            for column in ("decision_id", "amount_base", "raw_tx"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(sql.SQL("SELECT {} FROM executions").format(sql.Identifier(column)))
                conn.rollback()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT count(*) FROM decisions")
            conn.rollback()

    def test_the_watcher_role_cannot_write_reconciliations(self, watcher_url: str) -> None:
        with psycopg.connect(watcher_url) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "INSERT INTO reconciliations (subject_type, subject_id, state) "
                    "VALUES (%s, %s, %s)",
                    ("settlement", "1", "matched"),
                )
            conn.rollback()

    def test_the_reconciler_role_cannot_change_an_execution(
        self, reconciler_url: str, executor_config: ExecutorConfig, chain: FakeChain, seeded: dict
    ) -> None:
        """PRD: Reconciler hanya mencatat. GRANT-nya yang menegakkannya."""
        broadcast(executor_config, chain, seeded["decision_id"])
        with psycopg.connect(reconciler_url) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("UPDATE executions SET status = %s", ("failed",))
            conn.rollback()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("UPDATE decisions SET status = %s", ("denied",))
            conn.rollback()

    def test_the_reconciler_role_cannot_write_settlements(self, reconciler_url: str) -> None:
        with psycopg.connect(reconciler_url) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("UPDATE settlements SET status = %s", (STATUS_FINAL,))
            conn.rollback()


# --------------------------------------------------------------------------- #
# Kursor dan lock
# --------------------------------------------------------------------------- #


class TestTheCursor:
    def test_it_starts_absent_and_not_at_zero(self, executor_db: str) -> None:
        """Tidak adanya baris berarti belum ada blok yang dipindai.

        Kursor 0 berarti blok 0 **sudah** dipindai, dan blok 0 adalah blok
        yang sah. Keduanya tidak boleh tertukar.
        """
        assert cursor_value(executor_db) is None

    def test_it_only_advances_after_the_batch_is_stored(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Satu transaksi DB untuk batch dan kursornya (PRD v2 bagian 10 nomor 3)."""
        tx_hash = broadcast(executor_config, chain, seeded["decision_id"])
        chain.add_transfer(
            tx_hash=tx_hash, to_addr=RECIPIENT_A, amount_base=AMOUNT, block_number=BLOCK
        )
        deep_enough(chain)
        reports = watch(watcher_config, chain)

        assert cursor_value(executor_db) == reports[0].last_block
        assert len(settlement_rows(executor_db)) == 1

    def test_it_does_not_advance_past_the_safe_head(
        self, watcher_config: WatcherConfig, executor_db: str, chain: FakeChain
    ) -> None:
        chain.advance_to(100)
        watch(watcher_config, chain)
        value = cursor_value(executor_db)
        assert value is not None
        assert value == 100 - TEST_CONFIRMATIONS

    def test_nothing_is_scanned_before_the_depth_is_reached(
        self, watcher_config: WatcherConfig, executor_db: str
    ) -> None:
        chain = FakeChain()
        chain.advance_to(TEST_CONFIRMATIONS - 1)
        reports = watch(watcher_config, chain)
        assert reports[0].scanned is False
        assert cursor_value(executor_db) is None

    def test_from_block_skips_history_only_when_asked(
        self, watcher_config: WatcherConfig, executor_db: str, chain: FakeChain
    ) -> None:
        chain.advance_to(5000)
        watch(watcher_config, chain, from_block=4000)
        assert cursor_value(executor_db) == 4000 + watcher_config.scan_batch_size - 1

    def test_from_block_cannot_move_an_existing_cursor_forward(
        self, watcher_config: WatcherConfig, executor_db: str, chain: FakeChain
    ) -> None:
        """Memindahkannya ke depan akan melewatkan blok dalam diam."""
        chain.advance_to(5000)
        set_cursor(executor_db, 10)
        watch(watcher_config, chain, from_block=4000)
        value = cursor_value(executor_db)
        assert value is not None
        assert value < 4000


class TestFromBlockIsRequiredOnce:
    """M6.1: tanpa kursor dan tanpa ``--from-block``, Watcher berhenti.

    M6 memakai 0 sebagai default. Itu salah untuk Base Sepolia: dari genesis
    sampai kepala rantai ada puluhan juta blok, jadi kursornya praktis tidak
    pernah sampai ke pembayaran hari ini — dan Watcher yang terlihat jalan tapi
    tidak akan pernah tiba lebih buruk daripada Watcher yang menolak start.
    """

    def test_it_refuses_and_names_the_flag(
        self, watcher_config: WatcherConfig, chain: FakeChain, executor_db: str
    ) -> None:
        chain.advance_to(100)
        assert cursor_value(executor_db) is None
        runtime = open_watcher(watcher_config, client=chain)
        try:
            with pytest.raises(WatcherError, match="--from-block"):
                scan_once(runtime, from_block=None)
        finally:
            close_runtime(runtime)

    def test_the_refusal_writes_nothing(
        self, watcher_config: WatcherConfig, chain: FakeChain, executor_db: str
    ) -> None:
        """Berhenti berarti berhenti: tidak ada kursor dan tidak ada settlement."""
        chain.add_transfer(
            tx_hash=FOREIGN_TX, to_addr=RECIPIENT_B, amount_base=5 * USDC, block_number=BLOCK
        )
        deep_enough(chain)
        runtime = open_watcher(watcher_config, client=chain)
        try:
            with pytest.raises(WatcherError):
                scan_once(runtime, from_block=None)
        finally:
            close_runtime(runtime)

        assert cursor_value(executor_db) is None
        assert settlement_rows(executor_db) == []

    def test_run_watch_propagates_it_and_releases_the_lock(
        self, watcher_config: WatcherConfig, chain: FakeChain
    ) -> None:
        """``run_watch`` melempar; ``cli._cmd_watch`` yang mengubahnya jadi exit 1.

        Yang diperiksa di sini juga: lock sesinya dilepas walau siklusnya
        gagal. Tanpa itu, satu kesalahan pemakaian flag akan membuat Watcher
        berikutnya ditolak sampai prosesnya dimatikan.
        """
        chain.advance_to(100)
        out = io.StringIO()
        with pytest.raises(WatcherError, match="--from-block"):
            run_watch(watcher_config, client=chain, out=out)

        kedua = open_watcher(watcher_config, client=chain)
        close_runtime(kedua)

    def test_the_cli_catches_it(self) -> None:
        """``_cmd_watch`` menangkap ``WatcherError`` dan mengembalikan 1."""
        import inspect

        from payment_guard import cli

        sumber = inspect.getsource(cli._cmd_watch)
        assert "WatcherError" in sumber
        assert "return 1" in sumber

    def test_an_existing_cursor_ignores_the_flag_and_warns(
        self, watcher_config: WatcherConfig, chain: FakeChain, executor_db: str
    ) -> None:
        """Diabaikan **dan** diberitahukan. Diam-diam diabaikan sama buruknya."""
        chain.advance_to(5000)
        set_cursor(executor_db, 10)
        out = io.StringIO()
        reports = watch(watcher_config, chain, from_block=4000, out=out)

        assert reports[0].from_block_ignored == 4000
        assert reports[0].first_block == 11
        keluaran = out.getvalue()
        assert "PERINGATAN" in keluaran
        assert "DIABAIKAN" in keluaran
        assert "runbook.md" in keluaran

    def test_the_cursor_is_untouched_by_the_ignored_flag(
        self, watcher_config: WatcherConfig, chain: FakeChain, executor_db: str
    ) -> None:
        """Kursor maju dari tempatnya sendiri, bukan melompat ke ``--from-block``."""
        chain.advance_to(5000)
        set_cursor(executor_db, 10)
        watch(watcher_config, chain, from_block=4000)
        assert cursor_value(executor_db) == 10 + watcher_config.scan_batch_size

    def test_no_warning_when_the_flag_is_not_given(
        self, watcher_config: WatcherConfig, chain: FakeChain, executor_db: str
    ) -> None:
        chain.advance_to(5000)
        set_cursor(executor_db, 10)
        out = io.StringIO()
        reports = watch(watcher_config, chain, from_block=None, out=out)
        assert reports[0].from_block_ignored is None
        assert "PERINGATAN" not in out.getvalue()


class TestChunkedGetLogs:
    """Node publik menolak rentang lebar; rentangnya dibagi dua sampai diterima."""

    def seeded_chain(self, *, blocks: tuple[int, ...] = (5, 700, 1500)) -> FakeChain:
        """Rantai dengan transfer di beberapa blok yang berjauhan.

        Berjauhan dengan sengaja: rentang yang dipecah harus tetap mencakup
        semuanya, dan transfer yang berdekatan tidak akan membuktikan itu.
        """
        chain = FakeChain()
        for index, block in enumerate(blocks):
            chain.add_transfer(
                tx_hash="0x" + f"{index + 1:02x}" * 32,
                to_addr=RECIPIENT_B,
                amount_base=(index + 1) * USDC,
                block_number=block,
            )
        chain.advance_to(max(blocks) + TEST_CONFIRMATIONS)
        return chain

    def test_a_narrow_node_still_scans_every_block(
        self, watcher_config: WatcherConfig, executor_db: str
    ) -> None:
        """Node hanya menerima 100 blok sekali minta; tidak ada yang terlewat."""
        chain = self.seeded_chain()
        chain.max_log_range = 100
        lebar = dataclasses.replace(watcher_config, scan_batch_size=2000)

        reports = watch(lebar, chain, from_block=0)

        assert reports[0].ranges_fetched > 1
        assert reports[0].range_shrinks > 0
        assert len(settlement_rows(executor_db)) == 3

    def test_the_width_actually_shrinks_by_halving(
        self, watcher_config: WatcherConfig, executor_db: str
    ) -> None:
        chain = self.seeded_chain()
        chain.max_log_range = 100
        lebar = dataclasses.replace(watcher_config, scan_batch_size=1000)

        watch(lebar, chain, from_block=0)

        diminta = chain.log_range_widths
        assert diminta[0] == 1000
        # 1000 -> 500 -> 250 -> 125 -> 62, dan 62 diterima.
        assert diminta[:5] == [1000, 500, 250, 125, 62]

    def test_the_cursor_advances_per_range(
        self, watcher_config: WatcherConfig, executor_db: str
    ) -> None:
        """Aturan batch M6 tidak berubah: satu rentang, satu transaksi."""
        chain = self.seeded_chain()
        chain.max_log_range = 100
        lebar = dataclasses.replace(watcher_config, scan_batch_size=500)

        reports = watch(lebar, chain, from_block=0)

        assert cursor_value(executor_db) == reports[0].last_block
        assert reports[0].last_block == 499

    def test_rescanning_the_same_ranges_is_idempotent(
        self, watcher_config: WatcherConfig, executor_db: str
    ) -> None:
        chain = self.seeded_chain()
        chain.max_log_range = 100
        lebar = dataclasses.replace(watcher_config, scan_batch_size=2000)

        watch(lebar, chain, from_block=0)
        jumlah = len(settlement_rows(executor_db))
        set_cursor(executor_db, 0)
        watch(lebar, chain, from_block=0)

        assert len(settlement_rows(executor_db)) == jumlah == 3

    def test_a_non_range_error_is_raised_as_is(
        self, watcher_config: WatcherConfig, executor_db: str
    ) -> None:
        """Tidak diubah menjadi percobaan ulang.

        Node yang menolak karena filter salah akan menolak hal yang sama
        dengan rentang separuh — hanya lebih lambat, dan pesan akhirnya akan
        menyalahkan lebar rentang untuk masalah yang bukan itu.
        """
        chain = self.seeded_chain()
        chain.logs_failure = "invalid topic filter"

        runtime = open_watcher(watcher_config, client=chain)
        try:
            with pytest.raises(ChainError, match="invalid topic filter"):
                scan_once(runtime, from_block=0)
        finally:
            close_runtime(runtime)

        assert chain.log_range_widths == [watcher_config.scan_batch_size], "ada retry"
        assert cursor_value(executor_db) is None

    def test_a_node_that_refuses_even_the_floor_stops_clearly(
        self, watcher_config: WatcherConfig, executor_db: str
    ) -> None:
        """Di lebar 10 blok, penolakan hampir pasti bukan soal rentang."""
        chain = self.seeded_chain()
        chain.max_log_range = 1

        runtime = open_watcher(watcher_config, client=chain)
        try:
            with pytest.raises(WatcherError, match="lantainya"):
                scan_once(runtime, from_block=0)
        finally:
            close_runtime(runtime)

        assert cursor_value(executor_db) is None

    def test_the_attempt_limit_stops_without_advancing_the_cursor(
        self, watcher_config: WatcherConfig, executor_db: str
    ) -> None:
        """Batas percobaan tercapai: berhenti, dan kursor tidak maju."""
        chain = self.seeded_chain()
        chain.max_log_range = 20
        sempit = dataclasses.replace(watcher_config, scan_batch_size=1000, scan_max_attempts=2)

        runtime = open_watcher(sempit, client=chain)
        try:
            with pytest.raises(WatcherError, match="2 percobaan"):
                scan_once(runtime, from_block=0)
        finally:
            close_runtime(runtime)

        assert cursor_value(executor_db) is None
        assert settlement_rows(executor_db) == []

    def test_a_failure_mid_scan_keeps_the_ranges_already_stored(
        self, watcher_config: WatcherConfig, executor_db: str
    ) -> None:
        """Rentang yang sudah tersimpan tetap tersimpan, kursornya tetap maju.

        Yang penting bukan bahwa seluruh siklus atomik — ia tidak, dan tidak
        perlu. Yang penting adalah tidak ada blok yang ditandai sudah dipindai
        padahal log-nya belum tersimpan, dan tidak ada blok yang terlewat.
        """
        chain = self.seeded_chain(blocks=(5, 700))
        chain.max_log_range = 100
        lebar = dataclasses.replace(watcher_config, scan_batch_size=1000)

        # Rentang pertama berhasil, lalu node mulai menolak dengan error yang
        # bukan soal rentang.
        runtime = open_watcher(lebar, client=chain)
        try:
            asli = chain.transfer_logs
            panggilan = {"n": 0}

            def sekali_lalu_gagal(*args: Any, **kwargs: Any) -> Any:
                panggilan["n"] += 1
                if panggilan["n"] > 5:
                    raise ChainError("unauthorized: bad api key")
                return asli(*args, **kwargs)

            chain.transfer_logs = sekali_lalu_gagal  # type: ignore[method-assign]
            with pytest.raises(ChainError, match="unauthorized"):
                scan_once(runtime, from_block=0)
        finally:
            close_runtime(runtime)

        maju = cursor_value(executor_db)
        assert maju is not None, "rentang yang berhasil harus sudah tersimpan"
        assert maju < 999, "kursor tidak boleh melewati rentang yang gagal"
        # Tidak ada blok yang terlewat: semua yang di bawah kursor sudah dipindai.
        for row in settlement_rows(executor_db):
            assert row["block_number"] <= maju


class TestWalletAddressGuardAtStartup:
    """B3, DIKERJAKAN di M6.2 setelah keputusan CoS (opsi A).

    Di M6.1 kelas ini ada dengan tiga tes ber-``skip``: perilakunya butuh
    SELECT pada ``executions``, yang tidak dimiliki ``watcher_rw``. Migrasi
    ``0003_watcher_from_addr.sql`` memberi hak itu untuk **satu kolom**
    ``from_addr``, jadi ketiganya sekarang dijalankan — dan ``raise
    AssertionError`` yang dulu menjadi isinya diganti tes yang sebenarnya.

    Yang dijaga: konfigurasi yang menunjuk wallet lain tidak menggagalkan apa
    pun tanpa penjaga ini. Watcher jalan, menyaring log per pengirim yang
    salah, dan tidak pernah melihat satu pun pembayaran — tanpa pengecualian,
    tanpa baris ``settlements``, hanya diam.
    """

    OTHER_WALLET = "0x70997970c51812dc3a010c7d01b50e0d17dc79c8"

    def test_a_matching_wallet_starts_normally(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Ada eksekusi, alamatnya sama: lanjut."""
        broadcast(executor_config, chain, seeded["decision_id"])
        runtime = start_watcher(watcher_config, client=chain)
        close_runtime(runtime)

    def test_an_empty_executions_table_does_not_block_startup(
        self, watcher_config: WatcherConfig, chain: FakeChain, executor_db: str
    ) -> None:
        """Belum ada apa pun untuk dibandingkan, jadi tidak ada yang bisa salah.

        Menolak start di sini akan berarti Watcher tidak bisa dijalankan
        sebelum pembayaran pertama — padahal justru pembayaran pertama yang
        ingin dilihatnya.
        """
        assert count_executions(executor_db) == 0
        runtime = start_watcher(watcher_config, client=chain)
        close_runtime(runtime)

    def test_a_mismatched_wallet_refuses_to_start(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Ada ``from_addr`` yang berbeda: tolak, dan sebut kedua alamatnya."""
        broadcast(executor_config, chain, seeded["decision_id"])
        salah = dataclasses.replace(watcher_config, wallet_address=self.OTHER_WALLET)

        with pytest.raises(WatcherError) as info:
            start_watcher(salah, client=chain)

        pesan = str(info.value)
        assert self.OTHER_WALLET in pesan, "alamat yang diharapkan harus disebut"
        assert TEST_WALLET_ADDRESS in pesan, "alamat yang ditemukan harus disebut"
        assert "devonly_not_a_secret" not in pesan
        assert TEST_WALLET_SEED not in pesan, "kunci tidak pernah masuk pesan"

    def test_the_refusal_is_audited(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Audit di-commit sebelum pengecualian dilempar.

        Kejadian ini satu-satunya jejak yang akan ada: sesudahnya tidak ada
        apa pun yang berjalan.
        """
        broadcast(executor_config, chain, seeded["decision_id"])
        salah = dataclasses.replace(watcher_config, wallet_address=self.OTHER_WALLET)

        with pytest.raises(WatcherError):
            start_watcher(salah, client=chain)

        assert ("watcher", "alarm_watcher_wallet_mismatch") in audit_events(executor_db)
        payloads = audit_payloads_for(executor_db, "alarm_watcher_wallet_mismatch")
        assert len(payloads) == 1
        assert payloads[0]["expected_from_addr"] == self.OTHER_WALLET
        assert payloads[0]["found_from_addr"] == [TEST_WALLET_ADDRESS]
        assert payloads[0]["chain_id"] == CHAIN_ID

    def test_the_refusal_releases_the_session_lock(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Satu kesalahan konfigurasi tidak boleh menahan lock sampai reboot."""
        broadcast(executor_config, chain, seeded["decision_id"])
        salah = dataclasses.replace(watcher_config, wallet_address=self.OTHER_WALLET)

        with pytest.raises(WatcherError):
            start_watcher(salah, client=chain)

        kedua = open_watcher(watcher_config, client=chain)
        close_runtime(kedua)

    def test_nothing_is_scanned_when_it_refuses(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Penjaganya berjalan **sebelum** pemindaian apa pun."""
        tx_hash = broadcast(executor_config, chain, seeded["decision_id"])
        chain.add_transfer(
            tx_hash=tx_hash, to_addr=RECIPIENT_A, amount_base=AMOUNT, block_number=BLOCK
        )
        deep_enough(chain)
        salah = dataclasses.replace(watcher_config, wallet_address=self.OTHER_WALLET)

        with pytest.raises(WatcherError):
            start_watcher(salah, client=chain)

        assert settlement_rows(executor_db) == []
        assert cursor_value(executor_db) is None

    def test_it_runs_before_the_chain_is_asked_anything(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Konfigurasi yang sudah salah tidak perlu perjalanan RPC."""
        broadcast(executor_config, chain, seeded["decision_id"])
        chain.calls.clear()
        salah = dataclasses.replace(watcher_config, wallet_address=self.OTHER_WALLET)

        with pytest.raises(WatcherError):
            start_watcher(salah, client=chain)

        assert "chain_id" not in chain.calls
        assert "token_decimals" not in chain.calls

    def test_the_comparison_ignores_case(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Alamat ber-checksum EIP-55 di ``.env`` harus tetap cocok.

        Operator menyalin alamat dari explorer, dan explorer menampilkannya
        ber-checksum. Perbandingan yang peka huruf akan menolak start untuk
        alamat yang sama persis — kegagalan yang terlihat seperti bug penjaga
        dan bukan seperti konfigurasi yang benar.
        """
        from payment_guard.addresses import to_checksum_address

        broadcast(executor_config, chain, seeded["decision_id"])
        checksummed = to_checksum_address(TEST_WALLET_ADDRESS)
        assert checksummed != TEST_WALLET_ADDRESS, "alamat uji harus punya huruf besar"

        runtime = start_watcher(
            dataclasses.replace(watcher_config, wallet_address=checksummed), client=chain
        )
        close_runtime(runtime)

    def test_an_invalid_config_address_is_refused_clearly(
        self, watcher_config: WatcherConfig, chain: FakeChain
    ) -> None:
        """Alamat yang tidak sah ditolak dengan pesan yang menyebut variabelnya."""
        rusak = dataclasses.replace(watcher_config, wallet_address="0xbukan-alamat")

        with pytest.raises(WatcherError, match="EXECUTOR_WALLET_ADDRESS"):
            start_watcher(rusak, client=chain)

    def test_an_empty_config_address_is_refused(
        self, watcher_config: WatcherConfig, chain: FakeChain
    ) -> None:
        """Kosong juga tidak sah. Tidak ada default."""
        kosong = dataclasses.replace(watcher_config, wallet_address="")

        with pytest.raises(WatcherError, match="EXECUTOR_WALLET_ADDRESS"):
            start_watcher(kosong, client=chain)

    def test_several_foreign_addresses_are_all_reported(
        self, executor_db: str, watcher_config: WatcherConfig, chain: FakeChain, seeded: dict
    ) -> None:
        """Lebih dari satu alamat berarti wallet Executor pernah diganti."""
        lain = "0x3c44cdddb6a900fa2b585dd299e03d12fa4293bc"
        with psycopg.connect(executor_db) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO executions (decision_id, tx_hash, from_addr, to_addr, "
                "amount_base, nonce, raw_tx, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    seeded["decision_id"],
                    FOREIGN_TX,
                    lain,
                    USDC_ADDRESS,
                    str(AMOUNT),
                    0,
                    b"\x01",
                    "signed",
                ),
            )
            conn.commit()

        with pytest.raises(WatcherError) as info:
            start_watcher(watcher_config, client=chain)

        assert lain in str(info.value)


class TestTheWatcherReadsOnlyFromAddr:
    """Inti migrasi 0003: satu kolom boleh dibaca, sisanya tidak.

    Kelas ini menggantikan ``TestTheWatcherStillCannotReadExecutions`` dari
    M6.1, yang menyatakan ``watcher`` tidak punya hak apa pun pada
    ``executions``. Pernyataan itu benar sampai keputusan CoS dan sengaja akan
    gagal sesudahnya — itu memang gunanya. Yang dijaga sekarang bukan "tidak
    ada hak" melainkan "tepat satu kolom", dan itu pernyataan yang lebih sempit
    serta lebih berguna: yang penting bukan bahwa ``from_addr`` bisa dibaca,
    tapi bahwa ``raw_tx`` dan sisanya tetap tidak.
    """

    #: Kolom ``executions`` yang **tidak** boleh dibaca Watcher. Daftar
    #: eksplisit, bukan "semua kecuali": kolom baru yang suatu hari ditambahkan
    #: ke tabel ini harus dipertimbangkan satu per satu, bukan ikut lolos
    #: karena tidak ada di daftar.
    FORBIDDEN_COLUMNS = (
        "id",
        "decision_id",
        "tx_hash",
        "to_addr",
        "amount_base",
        "nonce",
        "raw_tx",
        "status",
        "created_at",
        "broadcast_at",
        "updated_at",
    )

    def test_the_matrix_gives_the_watcher_exactly_one_column(self) -> None:
        from payment_guard.privileges import TABLE_MATRIX

        grant = TABLE_MATRIX["executions"]["watcher"]
        assert grant.select_columns == ("from_addr",)
        assert grant.select is False, "SELECT tingkat tabel tidak boleh"
        assert grant.insert is False
        assert grant.update_columns is None

    def test_the_matrix_still_gives_the_watcher_nothing_on_decisions(self) -> None:
        """Yang berubah hanya ``executions``. ``decisions`` tetap tertutup."""
        from payment_guard.privileges import TABLE_MATRIX

        assert "watcher" not in TABLE_MATRIX["decisions"]

    def test_there_are_exactly_three_migrations(self) -> None:
        """0003 ada; 0004 tidak. Grant berikutnya butuh keputusan berikutnya."""
        akar = Path(__file__).resolve().parents[2]
        migrasi = sorted(p.name for p in (akar / "migrations").glob("*.sql"))
        assert migrasi == [
            "0001_init.sql",
            "0002_roles.sql",
            "0003_watcher_from_addr.sql",
        ], migrasi

    def test_watcher_app_can_select_from_addr(
        self, watcher_url: str, executor_config: ExecutorConfig, chain: FakeChain, seeded: dict
    ) -> None:
        broadcast(executor_config, chain, seeded["decision_id"])
        with psycopg.connect(watcher_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT from_addr FROM executions")
            assert [row[0] for row in cur.fetchall()] == [TEST_WALLET_ADDRESS]

    @pytest.mark.parametrize("column", FORBIDDEN_COLUMNS)
    def test_watcher_app_cannot_select_any_other_column(
        self, watcher_url: str, column: str
    ) -> None:
        """Tes utama migrasi ini, satu kolom per parametrisasi.

        Dijalankan tanpa baris apa pun di tabel dengan sengaja: Postgres
        memeriksa hak sebelum membaca data, jadi penolakannya tidak bergantung
        pada ada tidaknya isi.
        """
        with psycopg.connect(watcher_url) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(sql.SQL("SELECT {} FROM executions").format(sql.Identifier(column)))
            conn.rollback()

    def test_watcher_app_cannot_select_star(self, watcher_url: str) -> None:
        """``SELECT *`` adalah bentuk yang paling mudah ditulis tanpa sadar."""
        with psycopg.connect(watcher_url) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT * FROM executions")
            conn.rollback()

    def test_counting_rows_is_allowed_and_that_is_worth_knowing(self, watcher_url: str) -> None:
        """``count(*)`` **berhasil** dengan hak satu kolom. Itu mengejutkan.

        Postgres memperlakukan ``count(*)`` sebagai tidak merujuk kolom mana
        pun, jadi hak ``SELECT`` pada **satu** kolom sudah cukup. Hak kolom
        karena itu bukan penghalang untuk mengetahui **berapa banyak** baris
        yang ada — hanya untuk mengetahui apa isinya.

        Dicatat sebagai tes, bukan sebagai komentar, karena kesimpulan
        sebaliknya terasa lebih masuk akal: saya menulis tes ini dengan
        ``pytest.raises`` lebih dulu, dan ia gagal.

        Dampaknya pada model ancaman kecil dan perlu disebut jujur: jumlah
        eksekusi bukan rahasia, dan Watcher sudah mengetahui lebih banyak dari
        itu lewat ``settlements`` yang ia tulis sendiri. Yang tetap tertutup
        adalah isi setiap barisnya.
        """
        with psycopg.connect(watcher_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM executions")
            row = cur.fetchone()
            assert row is not None
            assert isinstance(row[0], int)

    def test_watcher_app_cannot_write_executions(self, watcher_url: str) -> None:
        """Hak baca satu kolom tidak membawa hak tulis apa pun."""
        with psycopg.connect(watcher_url) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("UPDATE executions SET from_addr = %s", (TEST_WALLET_ADDRESS,))
            conn.rollback()

    def test_watcher_app_still_cannot_read_decisions(self, watcher_url: str) -> None:
        with psycopg.connect(watcher_url) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT 1 FROM decisions")
            conn.rollback()

    def test_the_module_query_names_only_that_column(self) -> None:
        """Penjaga statis: query di modul tidak boleh menyebut kolom lain.

        Postgres akan menolaknya saat dijalankan, tapi penolakan itu muncul
        sebagai ``permission denied`` yang tidak menyebut sebabnya. Lebih baik
        tertangkap di sini.
        """
        from payment_guard.watcher import SELECT_EXECUTION_SENDERS

        teks = SELECT_EXECUTION_SENDERS.lower()
        assert "from_addr" in teks
        for column in self.FORBIDDEN_COLUMNS:
            assert column not in teks, column
        assert "count(" not in teks
        assert "*" not in teks


class TestOnlyOneWatcherPerChain:
    def test_a_second_watcher_is_refused(
        self, watcher_config: WatcherConfig, chain: FakeChain
    ) -> None:
        """Lock **sesi**, dan ``pg_try_advisory_lock``: gagal, bukan menunggu.

        Watcher kedua yang menunggu diam-diam adalah Watcher yang terlihat
        jalan tapi tidak memindai apa pun.
        """
        first = open_watcher(watcher_config, client=chain)
        try:
            with pytest.raises(WatcherError, match="sudah memegang lock"):
                open_watcher(watcher_config, client=chain)
        finally:
            close_runtime(first)

    def test_the_lock_is_released_when_the_runtime_closes(
        self, watcher_config: WatcherConfig, chain: FakeChain
    ) -> None:
        first = open_watcher(watcher_config, client=chain)
        close_runtime(first)
        second = open_watcher(watcher_config, client=chain)
        close_runtime(second)


class TestStartupPrintsTheMonitoredWallet:
    """Baris startup ``guard watch`` menyebut alamat yang dipantau.

    Barisnya sudah ada sejak M6, tapi tidak ada tes yang menjaganya. Sejak
    M6.3 ia **load-bearing**: checklist operator di ``docs/runbook.md`` bagian
    10 menyuruh membaca angka itu dan membandingkannya dengan alamat yang
    dicetak ``guard execute``, karena penjaga saat start baru bekerja setelah
    ada eksekusi pertama. Baris yang hilang akan membuat checklist itu
    menunjuk sesuatu yang tidak ada, tanpa satu pun tes gagal.

    Padanannya untuk Executor sudah dijaga di
    ``test_execution_flow.py`` (``TEST_WALLET_ADDRESS in keluaran``).
    """

    def test_the_configured_address_is_printed(
        self, watcher_config: WatcherConfig, chain: FakeChain
    ) -> None:
        out = io.StringIO()
        runtime = start_watcher(watcher_config, client=chain, out=out)
        close_runtime(runtime)
        keluaran = out.getvalue()
        assert watcher_config.wallet_address in keluaran
        assert "memantau transfer dari" in keluaran

    def test_it_prints_the_config_value_not_something_derived(
        self, watcher_config: WatcherConfig, chain: FakeChain
    ) -> None:
        """Yang dicetak harus ``EXECUTOR_WALLET_ADDRESS``, bukan nilai lain.

        Itu yang membuat perbandingan manual di checklist berarti: Executor
        mencetak alamat yang **diturunkan dari berkas kunci**, Watcher
        mencetak alamat dari **konfigurasi**. Dua sumber berbeda; kalau
        keduanya dicetak dari sumber yang sama, membandingkannya tidak
        membuktikan apa pun.
        """
        lain = "0x70997970c51812dc3a010c7d01b50e0d17dc79c8"
        out = io.StringIO()
        runtime = start_watcher(
            dataclasses.replace(watcher_config, wallet_address=lain), client=chain, out=out
        )
        close_runtime(runtime)
        assert lain in out.getvalue()

    def test_no_private_key_shape_reaches_stdout(
        self, watcher_config: WatcherConfig, chain: FakeChain
    ) -> None:
        """Alamat publik boleh dicetak; apa pun berbentuk kunci tidak.

        Bentuk yang dicari adalah ``0x`` + 64 hex, yaitu bentuk bahan kunci
        wallet. Watcher memang tidak pernah membaca berkas kunci, jadi tes ini
        menjaga sesuatu yang seharusnya tidak mungkin — dan itu justru
        gunanya: kalau suatu hari ia membacanya, kegagalannya muncul di sini.
        """
        out = io.StringIO()
        runtime = start_watcher(watcher_config, client=chain, out=out)
        close_runtime(runtime)
        keluaran = out.getvalue()
        assert TEST_WALLET_SEED not in keluaran
        assert re.search(r"0x[0-9a-fA-F]{64}", keluaran) is None, keluaran
        assert "devonly_not_a_secret" not in keluaran
        assert "postgresql://" not in keluaran


class TestStartupChecks:
    def test_a_wrong_chain_id_refuses_to_start(
        self, watcher_config: WatcherConfig, chain: FakeChain
    ) -> None:
        from payment_guard.chain import ChainError

        chain.chain_id_value = 1
        with pytest.raises(ChainError, match="chain id"):
            start_watcher(watcher_config, client=chain)

    def test_wrong_decimals_refuses_to_start(
        self, watcher_config: WatcherConfig, chain: FakeChain
    ) -> None:
        from payment_guard.chain import ChainError

        chain.decimals_value = 18
        with pytest.raises(ChainError, match="decimals"):
            start_watcher(watcher_config, client=chain)

    def test_a_refused_start_does_not_keep_the_lock(
        self, watcher_config: WatcherConfig, chain: FakeChain
    ) -> None:
        """Start yang gagal harus melepas koneksinya, bukan menahan lock.

        Tanpa ini, satu kesalahan konfigurasi akan membuat Watcher berikutnya
        ditolak sampai prosesnya dimatikan.
        """
        from payment_guard.chain import ChainError

        chain.chain_id_value = 1
        with pytest.raises(ChainError):
            start_watcher(watcher_config, client=chain)

        chain.chain_id_value = CHAIN_ID
        runtime = start_watcher(watcher_config, client=chain)
        close_runtime(runtime)

    def test_it_does_not_look_at_the_gas_balance(
        self, watcher_config: WatcherConfig, chain: FakeChain
    ) -> None:
        """Watcher tidak mengirim apa pun, jadi saldo gas bukan urusannya."""
        chain.balance_wei_value = 0
        runtime = start_watcher(watcher_config, client=chain)
        close_runtime(runtime)
        assert "eth_balance" not in chain.calls


class TestTheWatcherNeverLeaksSecrets:
    def test_the_database_url_never_reaches_stdout(
        self, watcher_config: WatcherConfig, chain: FakeChain
    ) -> None:
        out = io.StringIO()
        runtime = start_watcher(watcher_config, client=chain, out=out)
        try:
            scan_once(runtime, from_block=0, out=out)
        finally:
            close_runtime(runtime)
        assert "devonly_not_a_secret" not in out.getvalue()
        assert "postgresql://" not in out.getvalue()

    def test_the_from_block_refusal_does_not_leak_the_url_either(
        self, watcher_config: WatcherConfig, chain: FakeChain
    ) -> None:
        """Jalur gagal juga jalur yang pesannya dibaca operator."""
        chain.advance_to(100)
        runtime = open_watcher(watcher_config, client=chain)
        try:
            with pytest.raises(WatcherError) as info:
                scan_once(runtime, from_block=None)
        finally:
            close_runtime(runtime)
        pesan = str(info.value)
        assert "devonly_not_a_secret" not in pesan
        assert "postgresql://" not in pesan


# --------------------------------------------------------------------------- #
# M7: laporan baca saja dan kasus tepi rekonsiliasi
# --------------------------------------------------------------------------- #


def audit_count(url: str) -> int:
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_log")
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


class TestReportWritesNothing:
    """``guard reconcile --report`` tidak boleh menulis satu baris pun.

    Ini pernyataan yang paling penting tentang mode itu, dan ia tidak bisa
    dibuktikan dengan membaca kode saja: yang membuktikannya adalah
    membandingkan kedua tabel sebelum dan sesudah.
    """

    def siapkan_temuan(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        chain: FakeChain,
        decision_id: str,
    ) -> None:
        """Satu settlement final yang TIDAK cocok, supaya ada alarm."""
        tx_hash = broadcast(executor_config, chain, decision_id)
        chain.add_transfer(
            tx_hash=tx_hash, to_addr=RECIPIENT_B, amount_base=AMOUNT, block_number=BLOCK
        )
        deep_enough(chain)
        watch(watcher_config, chain, cycles=2)

    def test_it_leaves_reconciliations_and_audit_log_untouched(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        reconciler_config: ReconcilerConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        self.siapkan_temuan(executor_config, watcher_config, chain, seeded["decision_id"])
        sebelum_rekon = reconciliation_rows(executor_db)
        sebelum_audit = audit_count(executor_db)

        out = io.StringIO()
        laporan = report_once(reconciler_config, out=out)

        assert laporan.alarms, "tes ini butuh minimal satu alarm"
        assert reconciliation_rows(executor_db) == sebelum_rekon
        assert audit_count(executor_db) == sebelum_audit
        assert laporan.changed == 0

    def test_running_it_twice_still_writes_nothing(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        reconciler_config: ReconcilerConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        self.siapkan_temuan(executor_config, watcher_config, chain, seeded["decision_id"])
        sebelum_audit = audit_count(executor_db)

        report_once(reconciler_config, out=io.StringIO())
        report_once(reconciler_config, out=io.StringIO())

        assert audit_count(executor_db) == sebelum_audit
        assert reconciliation_rows(executor_db) == []

    def test_it_reports_findings_that_reconcile_has_never_recorded(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        reconciler_config: ReconcilerConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Inilah opsi A: hitungan segar, bukan isi tabel.

        ``reconciliations`` masih kosong — ``guard reconcile`` belum pernah
        dijalankan — tapi laporannya sudah menyebut temuannya.
        """
        self.siapkan_temuan(executor_config, watcher_config, chain, seeded["decision_id"])
        assert reconciliation_rows(executor_db) == []

        out = io.StringIO()
        report_once(reconciler_config, out=out)

        keluaran = out.getvalue()
        assert "mismatch" in keluaran
        assert "Perlu tindakan" in keluaran
        assert "hitungan segar" in keluaran

    def test_the_output_names_the_database_clock_and_chain(
        self, reconciler_config: ReconcilerConfig
    ) -> None:
        out = io.StringIO()
        report_once(reconciler_config, out=out)
        keluaran = out.getvalue()
        assert f"chain {CHAIN_ID}" in keluaran
        assert "UTC" in keluaran
        assert "jam database" in keluaran

    def test_the_output_leaks_nothing(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        reconciler_config: ReconcilerConfig,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """URL database, kunci, dan hash penuh tidak boleh muncul."""
        self.siapkan_temuan(executor_config, watcher_config, chain, seeded["decision_id"])
        out = io.StringIO()
        report_once(reconciler_config, out=out)
        keluaran = out.getvalue()

        assert "devonly_not_a_secret" not in keluaran
        assert "postgresql://" not in keluaran
        assert TEST_WALLET_SEED not in keluaran
        assert re.search(r"0x[0-9a-fA-F]{64}", keluaran) is None, keluaran
        assert keluaran.isascii()

    def test_the_exit_code_is_one_when_something_needs_action(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        reconciler_config: ReconcilerConfig,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        self.siapkan_temuan(executor_config, watcher_config, chain, seeded["decision_id"])
        assert run_report(reconciler_config, out=io.StringIO()) == 1

    def test_the_exit_code_is_zero_when_nothing_needs_action(
        self, reconciler_config: ReconcilerConfig
    ) -> None:
        assert run_report(reconciler_config, out=io.StringIO()) == 0

    def test_the_cli_flag_reaches_the_report_path(self) -> None:
        """``guard reconcile --report`` ada, dan bawaannya tetap menulis."""
        from payment_guard.cli import build_parser

        assert build_parser().parse_args(["reconcile", "--report"]).report is True
        assert build_parser().parse_args(["reconcile"]).report is False


class TestReconciliationEdgeCases:
    """Kasus tepi yang diminta M7, di luar yang sudah bertes sejak M6."""

    def test_a_settlement_seen_before_its_execution_flips_to_matched(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        reconciler_config: ReconcilerConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Urutan terbalik: Watcher melihat transfer sebelum ada execution-nya.

        Bisa terjadi karena Watcher dan Executor adalah dua proses: Watcher
        memindai blok yang sudah aman sementara baris ``executions`` baru
        ditulis sesudahnya, atau karena operator menjalankan reconcile di
        antara keduanya.

        Yang diuji: kesimpulan pertama ``unmatched_onchain`` **berubah**
        menjadi ``matched`` begitu execution-nya ada — bukan menetap sebagai
        alarm palsu.
        """
        # Transfer dengan hash yang akan dipakai execution nanti belum bisa
        # dibuat, jadi urutannya ditiru sebaliknya: execution dibuat, lalu
        # barisnya disembunyikan dari Reconciler dengan menghapusnya? Tidak —
        # yang dihapus tidak boleh. Sebagai gantinya: transfer asing dulu,
        # lalu execution dengan tx_hash yang sama disisipkan admin.
        tx_hash = FOREIGN_TX
        chain.add_transfer(
            tx_hash=tx_hash, to_addr=RECIPIENT_A, amount_base=AMOUNT, block_number=BLOCK
        )
        deep_enough(chain)
        watch(watcher_config, chain, cycles=2)

        pertama = reconcile_once(reconciler_config)
        assert [f.state for f in pertama.findings] == [STATE_UNMATCHED_ONCHAIN]
        assert reconciliation_rows(executor_db)[0]["state"] == STATE_UNMATCHED_ONCHAIN

        # Execution-nya menyusul, dengan hash yang sama.
        with psycopg.connect(executor_db) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO executions (decision_id, tx_hash, from_addr, to_addr, "
                "amount_base, nonce, raw_tx, status, broadcast_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())",
                (
                    seeded["decision_id"],
                    tx_hash,
                    TEST_WALLET_ADDRESS,
                    USDC_ADDRESS,
                    str(AMOUNT),
                    0,
                    b"\x01",
                    "broadcast",
                ),
            )
            conn.commit()

        kedua = reconcile_once(reconciler_config)

        assert [f.state for f in kedua.findings] == [STATE_MATCHED]
        assert kedua.changed == 1, "state harus berubah, bukan ditulis ulang"
        baris = reconciliation_rows(executor_db)
        assert len(baris) == 1, "satu baris per subjek, di-upsert"
        assert baris[0]["state"] == STATE_MATCHED

        # Kolom `execution_id` tetap NULL, dan itu BATAS HAK AKSES, bukan bug:
        # `reconciler_rw` hanya boleh meng-UPDATE (state, detail, updated_at)
        # di `0002_roles.sql`, jadi kolom tautan yang NULL saat INSERT pertama
        # tidak bisa diisi belakangan. Informasinya tidak hilang — ia pindah ke
        # `detail`, yang boleh di-UPDATE.
        assert baris[0]["execution_id"] is None, "kolomnya tidak bisa di-UPDATE"
        assert baris[0]["detail"]["execution_id"] is not None
        assert baris[0]["detail"]["decision_id"] == seeded["decision_id"]

    def test_the_flip_is_audited_as_a_state_change(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        reconciler_config: ReconcilerConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Perubahan kesimpulan harus terlihat di ``audit_log``."""
        self.test_a_settlement_seen_before_its_execution_flips_to_matched(
            executor_config, watcher_config, reconciler_config, executor_db, chain, seeded
        )
        payloads = audit_payloads_for(executor_db, "reconciliation_state_changed")
        state_akhir = [p for p in payloads if p["state"] == STATE_MATCHED]
        assert state_akhir, payloads
        assert state_akhir[-1]["previous_state"] == STATE_UNMATCHED_ONCHAIN

    def test_an_expired_decision_without_settlement_is_not_an_alarm(
        self, reconciler_config: ReconcilerConfig, executor_db: str, seeded: dict
    ) -> None:
        """Sudah bertes sejak M6; di sini yang diperiksa adalah ia bukan alarm.

        Laporan memisahkan ``expired`` dari daftar tindakan, jadi sifat "bukan
        alarm" itu sekarang punya konsekuensi yang terlihat operator.
        """
        with psycopg.connect(executor_db) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE decisions SET created_at = now() - make_interval(secs => %s), "
                "decided_at = now() - make_interval(secs => %s), "
                "approved_at = now() - make_interval(secs => %s), "
                "valid_until = now() - make_interval(secs => %s) WHERE id = %s",
                (10_800, 7200, 7200, 3600, seeded["decision_id"]),
            )
            conn.commit()

        out = io.StringIO()
        laporan = report_once(reconciler_config, out=out)

        assert [f.state for f in laporan.findings] == [STATE_EXPIRED]
        assert laporan.alarms == []
        keluaran = out.getvalue()
        assert "Tidak ada yang perlu tindakan" in keluaran
        assert "jatahnya sudah dilepas" in keluaran


class TestForbiddenTransitionsAreRefusedByTheDatabase:
    """Transisi terlarang ditolak, dan baris di database tidak berubah."""

    def pending_row(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        chain: FakeChain,
        decision_id: str,
    ) -> Any:
        tx_hash = broadcast(executor_config, chain, decision_id)
        chain.add_transfer(
            tx_hash=tx_hash, to_addr=RECIPIENT_A, amount_base=AMOUNT, block_number=BLOCK
        )
        deep_enough(chain)
        watch(watcher_config, chain, cycles=1)
        return tx_hash

    def row_of(self, executor_db: str) -> Any:
        from payment_guard.watcher import SettlementRow

        baris = settlement_rows(executor_db)[0]
        return SettlementRow(
            id=int(baris["id"]),
            tx_hash=str(baris["tx_hash"]),
            log_index=int(baris["log_index"]),
            block_number=int(baris["block_number"]),
            block_hash=str(baris["block_hash"]),
            status=str(baris["status"]),
        )

    @pytest.mark.parametrize("tujuan", [STATUS_PENDING, STATUS_FINAL])
    def test_a_final_row_cannot_go_back(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
        tujuan: str,
    ) -> None:
        """``final -> pending`` dan ``final -> final`` ditolak di Python.

        Keduanya ditolak sebelum database disentuh, jadi barisnya utuh.
        """
        from payment_guard.watcher import _set_status

        self.pending_row(executor_config, watcher_config, chain, seeded["decision_id"])
        watch(watcher_config, chain, cycles=1)
        baris = self.row_of(executor_db)
        assert baris.status == STATUS_FINAL

        runtime = open_watcher(watcher_config, client=chain)
        try:
            with pytest.raises(WatcherError, match="tidak diizinkan"):
                _set_status(runtime, baris, to=tujuan, event=Event.SETTLEMENT_FINALIZED)
        finally:
            close_runtime(runtime)

        assert settlement_rows(executor_db)[0]["status"] == STATUS_FINAL

    def test_a_reorged_row_cannot_jump_straight_to_final(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """``reorged -> final`` harus lewat ``pending`` dulu."""
        from payment_guard.watcher import _set_status

        tx_hash = self.pending_row(executor_config, watcher_config, chain, seeded["decision_id"])
        chain.reorg_block(BLOCK)
        chain.drop_transaction(tx_hash)
        watch(watcher_config, chain, cycles=1)
        baris = self.row_of(executor_db)
        assert baris.status == STATUS_REORGED

        runtime = open_watcher(watcher_config, client=chain)
        try:
            with pytest.raises(WatcherError, match="tidak diizinkan"):
                _set_status(runtime, baris, to=STATUS_FINAL, event=Event.SETTLEMENT_FINALIZED)
        finally:
            close_runtime(runtime)

        assert settlement_rows(executor_db)[0]["status"] == STATUS_REORGED

    def test_a_stale_row_status_writes_nothing(
        self,
        executor_config: ExecutorConfig,
        watcher_config: WatcherConfig,
        executor_db: str,
        chain: FakeChain,
        seeded: dict,
    ) -> None:
        """Penjaga tingkat baris: ``AND status = %s`` menolak baris yang sudah pindah.

        Transisinya sah, tapi ``row.status`` yang dipegang pemanggil sudah
        basi. UPDATE tidak menemukan baris, dan tidak ada audit yang ditulis.
        """
        import dataclasses as dc

        from payment_guard.watcher import _set_status

        self.pending_row(executor_config, watcher_config, chain, seeded["decision_id"])
        watch(watcher_config, chain, cycles=1)
        baris = self.row_of(executor_db)
        assert baris.status == STATUS_FINAL

        # Pemanggil mengira barisnya masih `pending`: transisi pending -> final
        # diizinkan, tapi barisnya sudah `final`.
        basi = dc.replace(baris, status=STATUS_PENDING)
        sebelum = audit_count(executor_db)

        runtime = open_watcher(watcher_config, client=chain)
        try:
            berubah = _set_status(runtime, basi, to=STATUS_FINAL, event=Event.SETTLEMENT_FINALIZED)
        finally:
            close_runtime(runtime)

        assert berubah is False
        assert audit_count(executor_db) == sebelum, "audit ditulis padahal tidak ada perubahan"
