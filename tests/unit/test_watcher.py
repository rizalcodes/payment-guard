"""Watcher: bagian yang bisa diuji tanpa database.

Perilaku yang butuh baris sungguhan — kursor, idempotensi, state machine,
reorg — diuji di ``tests/integration/test_watcher_flow.py`` terhadap Postgres
di cluster tes. Yang ada di berkas ini adalah hal yang tidak butuh database
sama sekali: dekode log, aritmetika blok awal, bentuk SQL, dan batas hak yang
bisa diperiksa dari sumbernya.

Tidak ada jaringan di berkas ini, dan tidak ada ``Web3Client`` yang dibuat.
"""

from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path
from typing import Any

import pytest

from fake_chain import TEST_WALLET_ADDRESS, FakeChain, fake_hash
from payment_guard import watcher
from payment_guard.chain import (
    TRANSFER_EVENT_SIGNATURE,
    TRANSFER_EVENT_TOPIC,
    TRANSFER_TOPIC_COUNT,
    WORD_BYTES,
    ChainError,
    TransferLog,
    decode_transfer_log,
)
from payment_guard.money import BaseUnits
from payment_guard.watcher import (
    NO_BLOCK_SCANNED,
    OPEN_STATUSES,
    SETTLEMENT_STATUSES,
    STATUS_FINAL,
    STATUS_PENDING,
    STATUS_REORGED,
    ScanReport,
    WatcherError,
    _initial_block,
)

ROOT = Path(__file__).resolve().parents[2]
WATCHER_SOURCE = ROOT / "src" / "payment_guard" / "watcher.py"
INIT_SQL = ROOT / "migrations" / "0001_init.sql"

RECIPIENT = "0x1111111111111111111111111111111111111111"
USDC = "0x036cbd53842c5426634e7929541ec2318f3dcf7e"
TX = "0x" + "ab" * 32


def topic_for(address: str) -> bytes:
    """Alamat sebagai satu word, seperti topic ``from``/``to`` di log asli."""
    padding = b"\x00" * (WORD_BYTES - 20)
    return padding + bytes.fromhex(address[2:])


def raw_log(**overrides: Any) -> dict[str, object]:
    """Satu log ``eth_getLogs`` mentah yang sah, bisa dirusak per field."""
    log: dict[str, object] = {
        "transactionHash": bytes.fromhex(TX[2:]),
        "logIndex": 0,
        "blockNumber": 7,
        "blockHash": bytes.fromhex(fake_hash("blok-7")[2:]),
        "topics": [
            TRANSFER_EVENT_TOPIC,
            topic_for(TEST_WALLET_ADDRESS),
            topic_for(RECIPIENT),
        ],
        "data": (25_000_000).to_bytes(WORD_BYTES, "big"),
    }
    log.update(overrides)
    return log


# --------------------------------------------------------------------------- #
# Status dan konstanta: harus sama dengan skema yang dikunci
# --------------------------------------------------------------------------- #


class TestStatusesMatchTheSchema:
    """``0001_init.sql`` tidak boleh diubah, jadi kodenya yang harus cocok."""

    def schema(self) -> str:
        return INIT_SQL.read_text(encoding="utf-8")

    def settlements_block(self) -> str:
        """Hanya blok ``CREATE TABLE settlements``.

        Dibatasi dengan sengaja: ``executions`` juga punya kolom ``status``
        dan ``amount_base``, dan mencari di seluruh berkas akan memeriksa
        tabel yang salah lalu lulus atau gagal karena alasan yang keliru.
        """
        text = self.schema()
        start = text.index("CREATE TABLE settlements")
        return text[start : text.index(");", start)]

    def test_every_status_appears_in_the_check(self) -> None:
        text = self.settlements_block()
        for status in SETTLEMENT_STATUSES:
            assert f"'{status}'" in text, status

    def test_the_check_has_exactly_these_three(self) -> None:
        """Daftarnya tertutup di kedua arah, bukan hanya satu arah."""
        match = re.search(r"CHECK \(status IN \(([^)]*)\)\)", self.settlements_block())
        assert match is not None, "bentuk CHECK status di settlements berubah"
        documented = set(re.findall(r"'([a-z]+)'", match.group(1)))
        assert documented == set(SETTLEMENT_STATUSES)

    def test_open_statuses_are_the_ones_that_can_still_change(self) -> None:
        assert set(OPEN_STATUSES) == {STATUS_PENDING, STATUS_REORGED}
        assert STATUS_FINAL not in OPEN_STATUSES

    def test_no_block_scanned_is_minus_one(self) -> None:
        """Blok 0 adalah blok yang sah, jadi 0 tidak bisa berarti 'belum ada'.

        Pola yang sama dengan ``NO_NONCE_USED`` di Executor, dan karena alasan
        yang sama: nilai pertama yang sah adalah 0.
        """
        assert NO_BLOCK_SCANNED == -1

    def test_the_cursor_column_cannot_hold_minus_one(self) -> None:
        """Karena itu 'belum ada' diwakili tidak adanya baris, bukan -1."""
        assert "last_scanned BIGINT NOT NULL CHECK (last_scanned >= 0)" in self.schema()


# --------------------------------------------------------------------------- #
# Blok awal: tempat satu kesalahan melewatkan pembayaran selamanya
# --------------------------------------------------------------------------- #


class TestInitialBlock:
    def test_an_existing_cursor_continues_after_it(self) -> None:
        assert _initial_block(41, from_block=None, safe_head=100) == 42

    def test_a_cursor_at_zero_means_block_zero_was_scanned(self) -> None:
        """Kursor 0 bukan 'belum ada': blok 0 sudah dipindai, lanjut ke 1."""
        assert _initial_block(0, from_block=None, safe_head=100) == 1

    def test_without_a_cursor_it_starts_at_genesis(self) -> None:
        """Lambat, tapi tidak pernah melewatkan apa pun dalam diam."""
        assert _initial_block(NO_BLOCK_SCANNED, from_block=None, safe_head=100) == 0

    def test_from_block_only_applies_without_a_cursor(self) -> None:
        """Memindahkan kursor yang sudah berjalan ke depan akan melewatkan blok.

        Itu satu-satunya kesalahan di modul ini yang tidak bisa diperbaiki
        belakangan: blok yang terlewat tidak akan pernah diminta lagi.
        """
        assert _initial_block(41, from_block=9_000_000, safe_head=9_999_999) == 42

    def test_from_block_is_used_when_there_is_no_cursor(self) -> None:
        assert _initial_block(NO_BLOCK_SCANNED, from_block=500, safe_head=9999) == 500

    def test_from_block_cannot_outrun_the_safe_head(self) -> None:
        """Satu lewat kepala aman berarti 'tidak ada yang dipindai siklus ini'."""
        assert _initial_block(NO_BLOCK_SCANNED, from_block=9999, safe_head=100) == 101

    def test_a_negative_from_block_is_refused(self) -> None:
        with pytest.raises(WatcherError, match="tidak boleh negatif"):
            _initial_block(NO_BLOCK_SCANNED, from_block=-1, safe_head=100)


# --------------------------------------------------------------------------- #
# Dekode log: masukan dari node adalah masukan tak terpercaya
# --------------------------------------------------------------------------- #


class TestTransferEventConstants:
    def test_the_topic_is_the_known_erc20_value(self) -> None:
        """Known-answer: topic Transfer sama di setiap kontrak ERC-20."""
        assert (
            "0x" + TRANSFER_EVENT_TOPIC.hex()
            == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        )

    def test_the_topic_is_a_full_word_not_a_selector(self) -> None:
        """Memotongnya seperti selector fungsi akan tidak pernah cocok."""
        assert len(TRANSFER_EVENT_TOPIC) == WORD_BYTES

    def test_the_signature_has_no_parameter_names(self) -> None:
        assert TRANSFER_EVENT_SIGNATURE == "Transfer(address,address,uint256)"
        assert " " not in TRANSFER_EVENT_SIGNATURE


class TestDecodeTransferLog:
    def test_a_well_formed_log_decodes(self) -> None:
        log = decode_transfer_log(raw_log(), chain_id=84532)
        assert log.chain_id == 84532
        assert log.tx_hash == TX
        assert log.log_index == 0
        assert log.block_number == 7
        assert log.from_addr == TEST_WALLET_ADDRESS
        assert log.to_addr == RECIPIENT
        assert log.amount_base == 25_000_000

    def test_addresses_come_back_lowercase(self) -> None:
        """Perbandingan dengan ``executions.to_addr`` adalah perbandingan string.

        Kalau salah satu sisi ber-checksum, perbandingannya akan gagal untuk
        alasan yang tidak ada hubungannya dengan pembayaran.
        """
        log = decode_transfer_log(raw_log(), chain_id=84532)
        assert log.to_addr == log.to_addr.lower()
        assert log.from_addr == log.from_addr.lower()

    def test_hex_strings_are_accepted_too(self) -> None:
        """web3 pernah mengembalikan ``HexBytes``, pernah ``str``."""
        log = decode_transfer_log(
            raw_log(transactionHash=TX, blockHash=fake_hash("blok-7")), chain_id=84532
        )
        assert log.tx_hash == TX

    def test_a_zero_amount_is_allowed(self) -> None:
        """ERC-20 mengizinkan transfer nol, dan ``settlements`` mencerminkan chain.

        ``base_units()`` menolak nol karena ia menjaga apa yang kami **kirim**;
        di sini yang dicatat adalah apa yang **terjadi**.
        """
        log = decode_transfer_log(raw_log(data=(0).to_bytes(WORD_BYTES, "big")), chain_id=84532)
        assert log.amount_base == 0

    def test_the_schema_allows_zero_but_executions_does_not(self) -> None:
        """Dua CHECK yang berbeda, dan perbedaannya bukan kelalaian.

        ``settlements`` mencatat apa yang terjadi di chain, di mana transfer
        nol sah. ``executions`` mencatat apa yang kami kirim, dan kami tidak
        pernah mengirim nol.
        """
        text = INIT_SQL.read_text(encoding="utf-8")
        checks = set(re.findall(r"CHECK \(amount_base ([<>=]+) 0\)", text))
        assert checks == {">=", ">"}, checks

    @pytest.mark.parametrize("count", [0, 1, 2, 4])
    def test_the_wrong_number_of_topics_is_refused(self, count: int) -> None:
        topics = [TRANSFER_EVENT_TOPIC] * count
        with pytest.raises(ChainError, match=f"{TRANSFER_TOPIC_COUNT} topic"):
            decode_transfer_log(raw_log(topics=topics), chain_id=84532)

    def test_a_foreign_topic0_is_refused(self) -> None:
        """Filter node tidak dipatuhi: lebih baik berhenti daripada menebak."""
        other = bytes(32)
        with pytest.raises(ChainError, match="bukan Transfer"):
            decode_transfer_log(
                raw_log(topics=[other, topic_for(TEST_WALLET_ADDRESS), topic_for(RECIPIENT)]),
                chain_id=84532,
            )

    def test_a_pending_log_is_refused(self) -> None:
        with pytest.raises(ChainError, match="pending"):
            decode_transfer_log(raw_log(blockNumber=None), chain_id=84532)

    def test_a_log_without_a_block_hash_is_refused(self) -> None:
        with pytest.raises(ChainError, match="pending"):
            decode_transfer_log(raw_log(blockHash=None), chain_id=84532)

    def test_a_log_without_an_index_is_refused(self) -> None:
        with pytest.raises(ChainError, match="logIndex"):
            decode_transfer_log(raw_log(logIndex=None), chain_id=84532)

    def test_an_unpadded_address_topic_is_refused(self) -> None:
        """12 byte awal yang bukan nol bukan alamat yang di-pad.

        Membaca 20 byte terakhirnya akan memberi alamat yang terlihat masuk
        akal, dan itu persis yang membuat kesalahan semacam ini mahal.
        """
        rusak = b"\xff" * 12 + bytes.fromhex(RECIPIENT[2:])
        with pytest.raises(ChainError, match="bukan nol"):
            decode_transfer_log(
                raw_log(topics=[TRANSFER_EVENT_TOPIC, rusak, topic_for(RECIPIENT)]),
                chain_id=84532,
            )

    def test_a_short_topic_is_refused(self) -> None:
        with pytest.raises(ChainError, match="byte"):
            decode_transfer_log(
                raw_log(topics=[TRANSFER_EVENT_TOPIC, b"\x01\x02", topic_for(RECIPIENT)]),
                chain_id=84532,
            )

    def test_a_data_field_of_the_wrong_width_is_refused(self) -> None:
        with pytest.raises(ChainError):
            decode_transfer_log(raw_log(data=b"\x01"), chain_id=84532)

    def test_a_malformed_block_hash_is_named_as_such(self) -> None:
        """Pesannya menyebut hash **blok**, bukan hash transaksi."""
        with pytest.raises(ChainError, match="hash blok"):
            decode_transfer_log(raw_log(blockHash=b"\x01\x02"), chain_id=84532)

    def test_the_unique_key_matches_the_constraint(self) -> None:
        log = decode_transfer_log(raw_log(), chain_id=84532)
        assert log.unique_key == (84532, TX, 0)
        assert "UNIQUE (chain_id, tx_hash, log_index)" in INIT_SQL.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Tiruan harus setia: filternya ada di sisi "node"
# --------------------------------------------------------------------------- #


class TestFakeChainFiltersLikeANode:
    """Tiruan yang mengembalikan semua log akan meluluskan Watcher yang lupa menyaring."""

    def chain(self) -> FakeChain:
        chain = FakeChain()
        chain.add_transfer(
            tx_hash="0x" + "11" * 32, to_addr=RECIPIENT, amount_base=1, block_number=5
        )
        chain.add_transfer(
            tx_hash="0x" + "22" * 32, to_addr=RECIPIENT, amount_base=2, block_number=9
        )
        chain.add_transfer(
            tx_hash="0x" + "33" * 32,
            to_addr=RECIPIENT,
            amount_base=3,
            block_number=9,
            from_addr="0x9999999999999999999999999999999999999999",
        )
        return chain

    def test_only_the_requested_sender_comes_back(self) -> None:
        chain = self.chain()
        logs = chain.transfer_logs(
            USDC, from_addr=TEST_WALLET_ADDRESS, first_block=0, last_block=100
        )
        assert {log.amount_base for log in logs} == {1, 2}

    def test_only_the_requested_range_comes_back(self) -> None:
        chain = self.chain()
        logs = chain.transfer_logs(USDC, from_addr=TEST_WALLET_ADDRESS, first_block=6, last_block=9)
        assert [log.amount_base for log in logs] == [2]

    def test_the_range_is_inclusive_at_both_ends(self) -> None:
        chain = self.chain()
        logs = chain.transfer_logs(USDC, from_addr=TEST_WALLET_ADDRESS, first_block=5, last_block=5)
        assert [log.amount_base for log in logs] == [1]

    def test_block_hash_is_none_beyond_the_head(self) -> None:
        chain = self.chain()
        assert chain.block_hash(chain.latest_block_value) is not None
        assert chain.block_hash(chain.latest_block_value + 1) is None

    def test_reorg_changes_the_hash_but_not_the_receipt(self) -> None:
        """Dua keadaan berbeda di PRD v2 bagian 10, jadi dua pembantu berbeda."""
        chain = self.chain()
        sebelum = chain.block_hash(9)
        chain.reorg_block(9)
        assert chain.block_hash(9) != sebelum
        assert chain.receipt("0x" + "22" * 32) is not None

    def test_dropping_a_transaction_removes_receipt_and_log(self) -> None:
        chain = self.chain()
        chain.drop_transaction("0x" + "22" * 32)
        assert chain.receipt("0x" + "22" * 32) is None
        logs = chain.transfer_logs(
            USDC, from_addr=TEST_WALLET_ADDRESS, first_block=0, last_block=100
        )
        assert {log.amount_base for log in logs} == {1}

    def test_moving_a_transaction_updates_both(self) -> None:
        chain = self.chain()
        chain.move_transaction("0x" + "22" * 32, to_block=12)
        receipt = chain.receipt("0x" + "22" * 32)
        assert receipt is not None
        assert receipt.block_number == 12
        logs = chain.transfer_logs(
            USDC, from_addr=TEST_WALLET_ADDRESS, first_block=12, last_block=12
        )
        assert [log.amount_base for log in logs] == [2]

    def test_it_satisfies_the_client_protocol(self) -> None:
        """Tiruan yang tidak lagi memenuhi protokol akan menguji hal lain."""
        from payment_guard.chain import ChainClient

        chain: ChainClient = FakeChain()
        assert chain.latest_block() == 0


# --------------------------------------------------------------------------- #
# Laporan
# --------------------------------------------------------------------------- #


class TestScanReport:
    def base(self, **overrides: Any) -> ScanReport:
        values: dict[str, Any] = {
            "chain_id": 84532,
            "latest_block": 100,
            "safe_head": 88,
            "first_block": 0,
            "last_block": 88,
            "logs_seen": 0,
            "settlements_inserted": 0,
            "finalized": 0,
            "moved": 0,
            "reorged": 0,
            "restored": 0,
            "alarms": 0,
        }
        values.update(overrides)
        return ScanReport(**values)

    def test_a_quiet_cycle_says_only_what_it_scanned(self) -> None:
        text = self.base().render()
        assert "0..88" in text
        assert "ALARM" not in text

    def test_an_unscanned_cycle_explains_itself(self) -> None:
        text = self.base(scanned=False).render()
        assert "kedalaman konfirmasi" in text

    def test_counts_appear_only_when_non_zero(self) -> None:
        """Laporan yang memuat nol di mana-mana berhenti dibaca."""
        text = self.base(finalized=2).render()
        assert "final: 2" in text
        assert "dipindah" not in text

    def test_an_alarm_is_impossible_to_miss(self) -> None:
        text = self.base(alarms=1, reorged=1).render()
        assert "ALARM" in text

    def test_a_rewind_is_reported(self) -> None:
        text = self.base(cursor_rewound_to=40).render()
        assert "dimundurkan ke 40" in text


# --------------------------------------------------------------------------- #
# Batas hak: diperiksa dari sumbernya, bukan hanya dipercaya
# --------------------------------------------------------------------------- #


class TestWatcherStaysInsideItsPrivileges:
    """Matriks di ``0002_roles.sql`` yang menegakkannya; tes ini yang menjelaskannya.

    GRANT akan menolak pelanggaran saat dijalankan. Tes ini menolaknya saat
    ditulis, dengan pesan yang menyebut alasannya — supaya kesalahan ini
    tertangkap sebelum seseorang menghabiskan sore mencari tahu kenapa
    Postgres menolak.
    """

    def source(self) -> str:
        return WATCHER_SOURCE.read_text(encoding="utf-8")

    def sql_literals(self) -> list[str]:
        """Semua string literal di modul, kecuali docstring."""
        tree = ast.parse(self.source())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    if isinstance(first.value.value, str):
                        docstrings.add(id(first.value))
        return [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ]

    @pytest.mark.parametrize("table", ["decisions", "executions", "reconciliations"])
    def test_it_never_names_a_table_it_cannot_touch(self, table: str) -> None:
        """Watcher tidak punya hak apa pun pada ketiga tabel ini.

        ``decisions`` dan ``executions``: tidak ada SELECT sekalipun, supaya
        pencocokan tidak bisa pindah ke proses yang juga bisa menulis
        ``settlements``. ``reconciliations``: milik Reconciler.
        """
        offenders = [sql for sql in self.sql_literals() if table in sql]
        assert offenders == [], offenders

    def test_it_never_writes_to_the_audit_table_directly(self) -> None:
        assert "INSERT INTO audit_log" not in self.source()

    def test_it_never_deletes(self) -> None:
        """Tidak ada role yang punya DELETE, dan baris reorged tidak dihapus."""
        for sql in self.sql_literals():
            assert "DELETE" not in sql.upper(), sql

    def test_every_update_touches_only_granted_columns(self) -> None:
        """Kolom yang di-UPDATE harus ada di GRANT untuk ``watcher_rw``."""
        from payment_guard.privileges import TABLE_MATRIX

        granted = TABLE_MATRIX["settlements"]["watcher"].update_columns
        assert granted is not None
        for sql in self.sql_literals():
            if "UPDATE settlements" not in sql:
                continue
            for column in re.findall(r"(\w+)\s*=", sql.split("WHERE")[0]):
                if column in {"now", "EXCLUDED"}:
                    continue
                assert column in granted, f"{column} tidak di-GRANT: {sql}"


class TestSqlIsStatic:
    """Tidak ada SQL yang dirakit dari string, dan tidak ada nilai yang di-inline."""

    KEYWORDS = ("SELECT", "INSERT", "UPDATE")

    def statements(self) -> list[str]:
        return [
            value
            for name, value in vars(watcher).items()
            if name.isupper()
            and isinstance(value, str)
            and any(word in value.upper() for word in self.KEYWORDS)
        ]

    def test_there_are_statements_to_check(self) -> None:
        """Penjaga: tanpa ini, filter yang terlalu sempit meluluskan semuanya."""
        assert len(self.statements()) >= 7, [s.split()[0] for s in self.statements()]

    def test_no_statement_is_an_f_string_remnant(self) -> None:
        for sql in self.statements():
            assert "{" not in sql, sql

    def test_values_go_through_parameters(self) -> None:
        for sql in self.statements():
            if "WHERE" in sql or "VALUES" in sql:
                assert "%s" in sql, sql

    def test_the_insert_is_idempotent(self) -> None:
        """Inti idempotensinya ada di SQL, bukan di pemeriksaan Python."""
        assert "ON CONFLICT (chain_id, tx_hash, log_index) DO NOTHING" in (
            watcher.INSERT_SETTLEMENT
        )

    def test_the_cursor_upsert_covers_both_directions(self) -> None:
        assert "ON CONFLICT (chain_id) DO UPDATE" in watcher.UPSERT_CURSOR

    def test_timestamps_come_from_the_database_clock(self) -> None:
        """Arsitektur v1.1 prinsip 5: jam database, bukan jam proses."""
        for sql in (watcher.MARK_FINAL, watcher.MARK_VERIFIED, watcher.MOVE_BLOCK):
            assert "now()" in sql
        assert "datetime" not in WATCHER_SOURCE.read_text(encoding="utf-8")

    def test_finalizing_guards_the_previous_status(self) -> None:
        """UPDATE yang tidak memeriksa status lama bisa menghidupkan baris mati."""
        assert "AND status = %s" in watcher.MARK_FINAL


class TestTransferLogIsPlainData:
    def test_it_is_frozen(self) -> None:
        log = TransferLog(
            chain_id=1,
            tx_hash=TX,
            log_index=0,
            block_number=1,
            block_hash=fake_hash("b"),
            from_addr=TEST_WALLET_ADDRESS,
            to_addr=RECIPIENT,
            amount_base=BaseUnits(1),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            log.block_number = 2  # type: ignore[misc]
