"""Normalisasi baris, checksum, dan pemeriksaan kontrol transaksi."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from payment_guard.sqltext import (
    blank_out_literals,
    checksum,
    find_transaction_control,
    normalize_newlines,
)

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


class TestNormalizeAndChecksum:
    def test_crlf_and_cr_become_lf(self) -> None:
        assert normalize_newlines("a\r\nb\rc\nd") == "a\nb\nc\nd"

    def test_bytes_and_str_agree(self) -> None:
        assert normalize_newlines(b"a\r\nb") == normalize_newlines("a\r\nb")

    def test_checksum_is_stable_across_line_endings(self) -> None:
        """Inti dari .gitattributes: checkout CRLF di Windows tidak mengubah checksum."""
        unix = "CREATE TABLE t (id int);\nSELECT 1;\n"
        windows = unix.replace("\n", "\r\n")
        assert checksum(unix) == checksum(windows)

    def test_checksum_matches_plain_sha256_of_normalized_text(self) -> None:
        text = "SELECT 1;\r\n"
        expected = hashlib.sha256(b"SELECT 1;\n").hexdigest()
        assert checksum(text) == expected

    def test_checksum_changes_when_content_changes(self) -> None:
        assert checksum("SELECT 1;\n") != checksum("SELECT 2;\n")

    def test_checksum_is_64_lowercase_hex(self) -> None:
        value = checksum("SELECT 1;\n")
        assert len(value) == 64
        assert value == value.lower()


class TestBlankOutLiterals:
    def test_line_count_is_preserved(self) -> None:
        sql = "SELECT 1; -- komentar\n/* dua\nbaris */\nSELECT 2;\n"
        assert blank_out_literals(sql).count("\n") == sql.count("\n")

    def test_length_is_preserved(self) -> None:
        sql = "SELECT 'abc'; /* x */ -- y\n"
        assert len(blank_out_literals(sql)) == len(sql)

    def test_nested_block_comments(self) -> None:
        stripped = blank_out_literals("/* a /* b */ c */ SELECT 1;")
        assert "SELECT 1;" in stripped
        assert "a" not in stripped and "b" not in stripped

    def test_dollar_quoted_body_is_removed(self) -> None:
        stripped = blank_out_literals("DO $$ BEGIN COMMIT; END $$;")
        assert "BEGIN" not in stripped
        assert "COMMIT" not in stripped

    def test_tagged_dollar_quote(self) -> None:
        stripped = blank_out_literals("DO $do$ BEGIN NULL; END $do$;")
        assert "BEGIN" not in stripped

    def test_dollar_number_is_not_a_dollar_quote(self) -> None:
        """$1 adalah placeholder parameter, bukan pembuka blok dolar."""
        stripped = blank_out_literals("SELECT $1; COMMIT;")
        assert "COMMIT" in stripped

    def test_doubled_single_quote_inside_string(self) -> None:
        stripped = blank_out_literals("SELECT 'it''s; COMMIT'; SELECT 2;")
        assert "COMMIT" not in stripped
        assert "SELECT 2;" in stripped

    def test_e_string_backslash_escape(self) -> None:
        stripped = blank_out_literals(r"SELECT E'a\'; COMMIT;'; SELECT 2;")
        assert "COMMIT" not in stripped

    def test_quoted_identifier(self) -> None:
        stripped = blank_out_literals('SELECT * FROM "commit table";')
        assert "commit" not in stripped


class TestFindTransactionControl:
    @pytest.mark.parametrize(
        "sql",
        [
            "BEGIN;",
            "begin;",
            "  BEGIN  ;",
            "COMMIT;",
            "ROLLBACK;",
            "ABORT;",
            "START TRANSACTION;",
            "SAVEPOINT a;",
            "RELEASE SAVEPOINT a;",
            "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;",
            "CREATE TABLE t (id int);\nCOMMIT;",
        ],
    )
    def test_detects_transaction_control(self, sql: str) -> None:
        assert find_transaction_control(sql), sql

    @pytest.mark.parametrize(
        "sql",
        [
            "CREATE TABLE t (id int);",
            (
                "CREATE FUNCTION f() RETURNS trigger LANGUAGE plpgsql AS $$\n"
                "BEGIN\n  RETURN NULL;\nEND;\n$$;"
            ),
            "DO $do$ BEGIN RAISE EXCEPTION 'nope'; END $do$;",
            "-- BEGIN di komentar\nSELECT 1;",
            "SELECT 'BEGIN';",
            "SET search_path = public;",
            "SET LOCAL lock_timeout = '5s';",
            "CREATE FUNCTION f() RETURNS int LANGUAGE sql BEGIN ATOMIC SELECT 1; END;",
        ],
    )
    def test_ignores_safe_sql(self, sql: str) -> None:
        assert find_transaction_control(sql) == [], sql

    def test_reports_line_number(self) -> None:
        sql = "SELECT 1;\nSELECT 2;\nCOMMIT;\n"
        assert find_transaction_control(sql) == [(3, "COMMIT")]

    def test_set_transaction_is_flagged_but_plain_set_is_not(self) -> None:
        assert find_transaction_control("SET TRANSACTION READ ONLY;")
        assert find_transaction_control("SET statement_timeout = 0;") == []


class TestRealMigrationFiles:
    """File migrasi yang dikirim harus lolos semua pemeriksaan statik."""

    @pytest.mark.parametrize("path", sorted(MIGRATIONS.glob("*.sql")), ids=lambda p: p.name)
    def test_no_transaction_control(self, path: Path) -> None:
        sql = normalize_newlines(path.read_bytes())
        assert find_transaction_control(sql) == []

    def test_init_contains_plpgsql_begin_that_must_not_be_flagged(self) -> None:
        """Penjaga regresi: 0001 memang memuat BEGIN di dalam blok $$."""
        sql = normalize_newlines((MIGRATIONS / "0001_init.sql").read_bytes())
        assert "BEGIN" in sql
        assert find_transaction_control(sql) == []

    def test_roles_contains_do_block_begin_that_must_not_be_flagged(self) -> None:
        sql = normalize_newlines((MIGRATIONS / "0002_roles.sql").read_bytes())
        assert "BEGIN" in sql
        assert find_transaction_control(sql) == []
