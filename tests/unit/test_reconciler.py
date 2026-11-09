"""Reconciler: bagian yang bisa diuji tanpa database.

Pencocokan yang sesungguhnya diuji di
``tests/integration/test_watcher_flow.py`` terhadap Postgres di cluster tes:
ia membandingkan tiga tabel, dan tiruan untuk tiga tabel adalah tiruan yang
menguji dirinya sendiri. Yang ada di sini adalah daftar tertutup, bentuk SQL,
laporan, dan batas hak yang bisa diperiksa dari sumbernya.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from payment_guard import reconciler
from payment_guard.reconciler import (
    ALARM_STATES,
    BROADCAST_STATUSES,
    RECONCILIATION_STATES,
    STATE_EXECUTED_NOT_SETTLED,
    STATE_EXPIRED,
    STATE_INVALIDATED,
    STATE_MATCHED,
    STATE_MISMATCH,
    STATE_UNMATCHED_ONCHAIN,
    SUBJECT_DECISION,
    SUBJECT_EXECUTION,
    SUBJECT_SETTLEMENT,
    SUBJECT_TYPES,
    Finding,
    ReconcileReport,
    ReconcilerError,
)

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "payment_guard" / "reconciler.py"
INIT_SQL = ROOT / "migrations" / "0001_init.sql"


def finding(state: str = STATE_MATCHED, **overrides: object) -> Finding:
    values: dict[str, object] = {
        "subject_type": SUBJECT_SETTLEMENT,
        "subject_id": "1",
        "state": state,
        "detail": {},
    }
    values.update(overrides)
    return Finding(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Daftar tertutup: harus sama dengan skema yang dikunci
# --------------------------------------------------------------------------- #


class TestStatesMatchTheSchema:
    def schema(self) -> str:
        return INIT_SQL.read_text(encoding="utf-8")

    def reconciliations_block(self) -> str:
        text = self.schema()
        start = text.index("CREATE TABLE reconciliations")
        return text[start : text.index(");", start)]

    def test_the_state_check_has_exactly_these_six(self) -> None:
        """Dua arah: tidak ada state di skema yang tidak ada di kode, dan sebaliknya."""
        block = self.reconciliations_block()
        match = re.search(r"state\s+TEXT NOT NULL CHECK \(state IN\s*\(([^)]*)\)", block)
        assert match is not None, "bentuk CHECK state di reconciliations berubah"
        documented = set(re.findall(r"'([a-z_]+)'", match.group(1)))
        assert documented == set(RECONCILIATION_STATES)

    def test_the_subject_check_has_exactly_these_three(self) -> None:
        block = self.reconciliations_block()
        match = re.search(
            r"subject_type\s+TEXT NOT NULL CHECK \(subject_type IN \(([^)]*)\)", block
        )
        assert match is not None
        documented = set(re.findall(r"'([a-z]+)'", match.group(1)))
        assert documented == set(SUBJECT_TYPES)

    def test_one_row_per_subject(self) -> None:
        assert "UNIQUE (subject_type, subject_id)" in self.reconciliations_block()

    def test_execution_statuses_exclude_signed(self) -> None:
        """``signed`` ada sebelum ada apa pun di jaringan (write-ahead).

        Tidak adanya settlement untuk baris ``signed`` karena itu bukan
        temuan — ia keadaan normal. Memasukkannya akan membuat setiap crash
        sebelum broadcast menjadi alarm.
        """
        assert "signed" not in BROADCAST_STATUSES
        assert "failed" not in BROADCAST_STATUSES
        assert set(BROADCAST_STATUSES) == {"broadcast", "confirmed"}


class TestWhichStatesAreAlarms:
    def test_matched_is_not_an_alarm(self) -> None:
        assert STATE_MATCHED not in ALARM_STATES

    def test_expired_is_not_an_alarm(self) -> None:
        """Jatah yang dilepas karena kedaluwarsa adalah jalur normal.

        PRD v2 bagian 11 menyebutnya tanpa kata "alarm", berbeda dari
        ``unmatched_onchain`` dan ``mismatch`` yang disebut alarm eksplisit.
        """
        assert STATE_EXPIRED not in ALARM_STATES

    @pytest.mark.parametrize(
        "state",
        [STATE_UNMATCHED_ONCHAIN, STATE_MISMATCH, STATE_EXECUTED_NOT_SETTLED, STATE_INVALIDATED],
    )
    def test_the_rest_are_alarms(self, state: str) -> None:
        assert state in ALARM_STATES
        assert finding(state).is_alarm

    def test_every_alarm_state_is_a_real_state(self) -> None:
        assert ALARM_STATES <= set(RECONCILIATION_STATES)


# --------------------------------------------------------------------------- #
# Finding: menolak nilai yang tidak akan diterima database
# --------------------------------------------------------------------------- #


class TestFindingValidation:
    """Ditolak di Python, bukan hanya oleh CHECK.

    Dua lapis, dan lapisan Python ada supaya pesannya menyebut nama state yang
    salah — bukan ``violates check constraint "reconciliations_state_check"``.
    """

    def test_an_unknown_state_is_refused(self) -> None:
        with pytest.raises(ReconcilerError, match="state rekonsiliasi tidak dikenal"):
            finding("hampir_cocok")

    def test_an_unknown_subject_type_is_refused(self) -> None:
        with pytest.raises(ReconcilerError, match="jenis subjek tidak dikenal"):
            finding(subject_type="pembayaran")

    def test_a_valid_finding_keeps_its_links(self) -> None:
        item = finding(
            STATE_MISMATCH,
            settlement_id=7,
            execution_id=9,
            decision_id="2a1a0000-0000-4000-8000-000000000000",
        )
        assert item.settlement_id == 7
        assert item.execution_id == 9
        assert item.decision_id is not None

    def test_links_default_to_none(self) -> None:
        """Subjek yang tidak punya padanan tidak boleh mengarang padanan."""
        item = finding(STATE_UNMATCHED_ONCHAIN)
        assert item.execution_id is None
        assert item.decision_id is None


# --------------------------------------------------------------------------- #
# Laporan
# --------------------------------------------------------------------------- #


class TestReconcileReport:
    def test_an_empty_report_says_so(self) -> None:
        text = ReconcileReport().render()
        assert "0 subjek" in text
        assert "tidak ada alarm" in text

    def test_counts_are_per_state(self) -> None:
        report = ReconcileReport(
            findings=[finding(), finding(subject_id="2"), finding(STATE_MISMATCH, subject_id="3")]
        )
        counts = report.counts()
        assert counts[STATE_MATCHED] == 2
        assert counts[STATE_MISMATCH] == 1
        assert counts[STATE_EXPIRED] == 0

    def test_alarms_are_listed_one_per_line(self) -> None:
        """Angka "3 mismatch" tidak bisa ditindaklanjuti; subject_id-nya bisa."""
        report = ReconcileReport(
            findings=[finding(STATE_MISMATCH, subject_id="41"), finding(subject_id="42")]
        )
        text = report.render()
        assert "ALARM (1)" in text
        assert "41" in text
        assert "tidak ada alarm" not in text

    def test_matched_rows_are_counted_but_not_alarmed(self) -> None:
        report = ReconcileReport(findings=[finding(), finding(subject_id="2")])
        assert report.alarms == []
        assert "tidak ada alarm" in report.render()

    def test_changed_is_reported_separately_from_total(self) -> None:
        """Siklus yang tidak mengubah apa pun tetap memeriksa semuanya."""
        report = ReconcileReport(findings=[finding()], changed=0)
        assert "1 subjek diperiksa, 0 berubah" in report.render()


# --------------------------------------------------------------------------- #
# Batas hak dan bentuk SQL
# --------------------------------------------------------------------------- #


class TestReconcilerOnlyReads:
    """PRD: "Reconciler tidak pernah mengubah status decision atau execution."

    ``0002_roles.sql`` hanya memberinya SELECT pada keduanya, jadi pelanggaran
    akan ditolak database saat dijalankan. Tes ini menolaknya saat ditulis.
    """

    def source(self) -> str:
        return SOURCE.read_text(encoding="utf-8")

    def sql_literals(self) -> list[str]:
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

    @pytest.mark.parametrize("table", ["decisions", "executions", "settlements", "watcher_state"])
    def test_it_never_writes_to_a_table_it_only_reads(self, table: str) -> None:
        for sql in self.sql_literals():
            upper = sql.upper()
            for verb in ("INSERT INTO", "UPDATE", "DELETE FROM"):
                if verb in upper:
                    assert table not in sql, f"{verb} menyentuh {table}: {sql}"

    def test_the_only_table_it_writes_is_reconciliations(self) -> None:
        written = set()
        for sql in self.sql_literals():
            for match in re.finditer(r"(?:INSERT INTO|UPDATE)\s+(\w+)", sql):
                written.add(match.group(1))
        assert written == {"reconciliations"}, written

    def test_it_never_deletes(self) -> None:
        for sql in self.sql_literals():
            assert "DELETE" not in sql.upper(), sql

    def test_its_update_touches_only_granted_columns(self) -> None:
        from payment_guard.privileges import TABLE_MATRIX

        granted = TABLE_MATRIX["reconciliations"]["reconciler"].update_columns
        assert granted is not None
        for column in re.findall(r"(\w+)\s*=", reconciler.UPDATE_RECONCILIATION.split("WHERE")[0]):
            if column == "now":
                continue
            assert column in granted, column

    def test_it_never_touches_the_chain(self) -> None:
        """Membandingkan tabel dengan tabel; bertanya ke RPC akan jadi sumber kedua."""
        text = self.source()
        assert "Web3Client" not in text
        assert "ChainClient" not in text
        assert "rpc_url" not in text


class TestSqlIsStatic:
    KEYWORDS = ("SELECT", "INSERT", "UPDATE")

    def statements(self) -> list[str]:
        return [
            value
            for name, value in vars(reconciler).items()
            if name.isupper()
            and isinstance(value, str)
            and any(word in value.upper() for word in self.KEYWORDS)
        ]

    def test_there_are_statements_to_check(self) -> None:
        assert len(self.statements()) >= 6, [s.split()[0] for s in self.statements()]

    def test_no_statement_is_an_f_string_remnant(self) -> None:
        for sql in self.statements():
            assert "{" not in sql, sql

    def test_the_deadline_stays_a_parameter(self) -> None:
        """``make_interval(secs => %s)``, bukan ``INTERVAL '3600 seconds'``.

        Merakit interval dari angka berarti SQL yang disusun dari string, yang
        dilarang di repo ini — dan di sini angkanya datang dari konfigurasi.
        """
        assert "make_interval(secs => %s)" in reconciler.SELECT_UNSETTLED_EXECUTIONS
        assert "INTERVAL '" not in reconciler.SELECT_UNSETTLED_EXECUTIONS

    def test_the_deadline_is_measured_by_the_database_clock(self) -> None:
        assert "now()" in reconciler.SELECT_UNSETTLED_EXECUTIONS
        assert "now()" in reconciler.SELECT_EXPIRED_DECISIONS

    def test_the_settlement_join_is_left(self) -> None:
        """Settlement tanpa execution adalah temuan terpenting modul ini.

        Join yang dalam akan membuangnya, dan tesnya akan tetap lulus karena
        tidak ada baris yang salah — hanya baris yang hilang.
        """
        assert "LEFT JOIN executions" in reconciler.SELECT_SETTLEMENTS

    def test_it_reads_finalized_at_to_tell_two_reorged_rows_apart(self) -> None:
        """Hanya yang pernah ``final`` yang menjadi ``invalidated``."""
        assert "finalized_at" in reconciler.SELECT_SETTLEMENTS

    def test_duplicate_final_logs_are_detected(self) -> None:
        """PRD v2 bagian 11 menuntut "tepat satu log Transfer yang cocok"."""
        assert "HAVING count(*) > 1" in reconciler.SELECT_FINAL_LOG_COUNTS


# --------------------------------------------------------------------------- #
# M7: laporan baca saja
# --------------------------------------------------------------------------- #


class TestShortHash:
    """Hash dipotong supaya satu baris laporan muat di satu layar.

    Yang dipotong adalah **tampilannya**, bukan yang disimpan: baris
    ``reconciliations`` dan ``audit_log`` tetap memuat hash penuh.
    """

    FULL = "0x9a6280ecc159423cb00c6a43457ddf252ad1be2c89b69c2b068fc378daa95200"

    def test_it_keeps_both_ends(self) -> None:
        pendek = reconciler.short_hash(self.FULL)
        assert pendek.startswith(self.FULL[: reconciler.SHORT_HASH_HEAD])
        assert pendek.endswith(self.FULL[-reconciler.SHORT_HASH_TAIL :])
        assert "..." in pendek

    def test_it_is_much_shorter_than_the_original(self) -> None:
        assert len(reconciler.short_hash(self.FULL)) < len(self.FULL) // 2

    def test_a_short_value_is_left_alone(self) -> None:
        """Tidak ada yang dipotong kalau memotongnya tidak menolong."""
        assert reconciler.short_hash("0xabc") == "0xabc"

    def test_it_stays_ascii(self) -> None:
        """Pemisah ASCII, bukan elipsis Unicode.

        Laporan ini dibaca di konsol operator, yang di Windows bisa memakai
        cp1252. Satu karakter non-ASCII di setiap baris adalah satu cara lagi
        perintah ini bisa gagal di tempat yang tidak ada hubungannya dengan
        rekonsiliasi.
        """
        assert reconciler.short_hash(self.FULL).isascii()


class TestReportRendering:
    """Laporan ``guard reconcile --report``: satu layar, bisa ditindaklanjuti."""

    TX = "0x9a6280ecc159423cb00c6a43457ddf252ad1be2c89b69c2b068fc378daa95200"

    def temuan(self, state: str, subject_id: str = "1", **detail: object) -> Finding:
        subjek = {
            STATE_EXECUTED_NOT_SETTLED: SUBJECT_EXECUTION,
            STATE_EXPIRED: SUBJECT_DECISION,
        }.get(state, SUBJECT_SETTLEMENT)
        return Finding(subjek, subject_id, state, dict(detail))

    def render(self, findings: list[Finding]) -> str:
        return reconciler.render_report(findings, chain_id=84532, db_time="2026-11-09 14:02:11 UTC")

    def test_the_header_names_the_chain_and_the_database_clock(self) -> None:
        teks = self.render([])
        assert "chain 84532" in teks
        assert "2026-11-09 14:02:11 UTC" in teks
        assert "jam database" in teks

    def test_the_read_only_line_is_always_printed(self) -> None:
        """Dicetak bahkan untuk laporan kosong.

        Tanpa baris itu, laporan yang berbeda dari tabel ``reconciliations``
        akan terlihat seperti kesalahan — padahal justru itu gunanya.
        """
        for findings in ([], [self.temuan(STATE_MATCHED)]):
            teks = self.render(findings)
            assert "Mode baca saja" in teks
            assert "hitungan segar" in teks
            assert "bukan isi tabel reconciliations" in teks
            assert "Tidak ada yang ditulis" in teks

    def test_every_state_appears_in_the_summary_even_at_zero(self) -> None:
        """Nol yang terlihat berbeda dari state yang lupa dihitung."""
        teks = self.render([self.temuan(STATE_MATCHED)])
        for state in RECONCILIATION_STATES:
            assert state in teks, state

    def test_alarm_states_are_marked_in_the_summary(self) -> None:
        teks = self.render([])
        ditandai: dict[str, bool] = {}
        for baris in teks.splitlines():
            bagian = baris.strip().split()
            if not bagian:
                continue
            if bagian[0] == "!" and len(bagian) > 1:
                ditandai[bagian[1]] = True
            elif bagian[0] in RECONCILIATION_STATES:
                ditandai[bagian[0]] = False

        assert set(ditandai) == set(RECONCILIATION_STATES), ditandai
        for state in RECONCILIATION_STATES:
            assert ditandai[state] is (state in ALARM_STATES), state

    def test_an_empty_report_says_nothing_needs_action(self) -> None:
        assert "Tidak ada yang perlu tindakan" in self.render([])

    def test_only_matched_and_expired_needs_no_action(self) -> None:
        teks = self.render([self.temuan(STATE_MATCHED), self.temuan(STATE_EXPIRED, "d1")])
        assert "Tidak ada yang perlu tindakan" in teks
        assert "Perlu tindakan" not in teks

    @pytest.mark.parametrize("state", sorted(ALARM_STATES))
    def test_every_alarm_state_reaches_the_action_list(self, state: str) -> None:
        """Keempat state alarm, bukan hanya tiga yang disebut brief.

        ``invalidated`` juga alarm: ia settlement yang pernah ``final`` lalu
        ter-reorg. Menghilangkannya dari daftar tindakan berarti menyembunyikan
        pembayaran yang berhenti berlaku.
        """
        teks = self.render([self.temuan(state, tx_hash=self.TX)])
        assert "Perlu tindakan (1)" in teks
        assert state in teks.split("Perlu tindakan")[1]

    def test_a_mismatch_says_which_fields_differ(self) -> None:
        teks = self.render(
            [self.temuan(STATE_MISMATCH, tx_hash=self.TX, differs=["to_addr", "amount_base"])]
        )
        assert "beda: to_addr, amount_base" in teks

    def test_an_unsettled_execution_says_the_deadline(self) -> None:
        teks = self.render(
            [self.temuan(STATE_EXECUTED_NOT_SETTLED, tx_hash=self.TX, deadline_seconds=3600)]
        )
        assert "lewat batas 3600 detik" in teks

    def test_an_invalidated_row_says_why(self) -> None:
        teks = self.render([self.temuan(STATE_INVALIDATED, tx_hash=self.TX)])
        assert "pernah final lalu ter-reorg" in teks

    def test_expired_is_explained_as_normal(self) -> None:
        teks = self.render([self.temuan(STATE_EXPIRED, "d1")])
        assert "tidak perlu tindakan" in teks
        assert "jatahnya sudah dilepas" in teks

    # -- Yang tidak boleh ada di keluaran ---------------------------------- #

    def test_no_full_hash_reaches_the_report(self) -> None:
        teks = self.render([self.temuan(STATE_MISMATCH, tx_hash=self.TX)])
        assert self.TX not in teks
        assert reconciler.short_hash(self.TX) in teks

    def test_the_report_is_ascii_only(self) -> None:
        """Konsol operator bisa cp1252; satu karakter aneh cukup untuk gagal."""
        teks = self.render(
            [
                self.temuan(STATE_MISMATCH, tx_hash=self.TX, differs=["to_addr"]),
                self.temuan(STATE_EXPIRED, "d1"),
                self.temuan(STATE_MATCHED, "9"),
            ]
        )
        assert teks.isascii(), [c for c in teks if not c.isascii()]

    def test_it_fits_one_screen(self) -> None:
        """Laporan yang tidak muat satu layar berhenti dibaca."""
        banyak = [self.temuan(STATE_MATCHED, str(i)) for i in range(200)]
        banyak += [self.temuan(STATE_EXPIRED, f"d{i}") for i in range(50)]
        teks = self.render(banyak)
        baris = teks.splitlines()
        assert len(baris) <= 24, len(baris)
        assert max(len(b) for b in baris) <= 100, max(len(b) for b in baris)

    def test_nothing_that_looks_like_a_secret_is_printed(self) -> None:
        teks = self.render([self.temuan(STATE_MISMATCH, tx_hash=self.TX)])
        for dilarang in ("postgresql://", "http://", "https://", "RPC_URL", "private_key"):
            assert dilarang not in teks, dilarang


class TestTheReportPathNeverWrites:
    """Penjaga statis: jalur ``--report`` tidak boleh memanggil ``record``.

    Yang sesungguhnya membuktikannya adalah tes integrasi yang membandingkan
    ``reconciliations`` dan ``audit_log`` sebelum dan sesudah. Tes ini
    menangkapnya lebih awal, dan menyebut sebabnya.
    """

    def source_of(self, name: str) -> str:
        import inspect

        return inspect.getsource(getattr(reconciler, name))

    def test_report_once_does_not_call_record(self) -> None:
        sumber = self.source_of("report_once")
        assert "record(" not in sumber
        assert "collect_findings(" in sumber

    def test_run_report_does_not_call_reconcile_once(self) -> None:
        sumber = self.source_of("run_report")
        assert "reconcile_once" not in sumber
        assert "report_once" in sumber

    def test_the_clock_query_reads_only(self) -> None:
        sql = reconciler.SELECT_DB_CLOCK.upper()
        assert sql.strip().startswith("SELECT")
        for verb in ("INSERT", "UPDATE", "DELETE"):
            assert verb not in sql

    def test_the_clock_comes_from_the_database(self) -> None:
        """Arsitektur v1.1 prinsip 5: jam database, bukan jam proses."""
        assert "now()" in reconciler.SELECT_DB_CLOCK
        assert "datetime" not in reconciler.SELECT_DB_CLOCK
