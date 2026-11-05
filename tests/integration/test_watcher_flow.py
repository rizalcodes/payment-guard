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

from conftest_executor import (
    CHAIN_ID,
    audit_events,
    seed_approved_decision,
    seed_mandate,
)
from conftest_guard import RECIPIENT_A, RECIPIENT_B, USDC
from fake_chain import TEST_WALLET_ADDRESS, FakeChain
from payment_guard.config import ExecutorConfig, ReconcilerConfig, WatcherConfig
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
)
from payment_guard.watcher import (
    STATUS_FINAL,
    STATUS_PENDING,
    STATUS_REORGED,
    WatcherError,
    close_runtime,
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
    from_block: int | None = None,
) -> list[Any]:
    """Jalankan ``cycles`` siklus Watcher pada runtime yang sama."""
    runtime = open_watcher(config, client=chain)
    try:
        return [
            scan_once(runtime, from_block=from_block if index == 0 else None)
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

    def test_the_watcher_role_cannot_even_select_executions(
        self, watcher_url: str, executor_db: str, chain: FakeChain, seeded: dict
    ) -> None:
        """Bukan hanya tidak menulis: ia tidak boleh **membaca**.

        Pencocokan adalah pekerjaan Reconciler. Proses yang bisa membaca
        ``executions`` dan menulis ``settlements`` sekaligus adalah proses
        yang bisa, suatu hari, "memperbaiki" salah satunya.
        """
        with psycopg.connect(watcher_url) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT count(*) FROM executions")
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
            scan_once(runtime, out=out)
        finally:
            close_runtime(runtime)
        assert "devonly_not_a_secret" not in out.getvalue()
        assert "postgresql://" not in out.getvalue()
