"""Penemuan dan validasi file migrasi tanpa menyentuh database.

Celah nomor, nomor ganda, nama file salah, dan kontrol transaksi di dalam file
semuanya harus gagal sebelum satu pun pernyataan dikirim ke Postgres.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from payment_guard.db import MigrationError
from payment_guard.migrate import discover, migrations_dir


def write(directory: Path, name: str, body: str = "SELECT 1;\n") -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


class TestHappyPath:
    def test_reads_versions_names_and_checksums(self, tmp_path: Path) -> None:
        write(tmp_path, "0001_init.sql", "CREATE TABLE a (id int);\n")
        write(tmp_path, "0002_roles.sql", "CREATE TABLE b (id int);\n")
        files = discover(tmp_path)
        assert [f.version for f in files] == [1, 2]
        assert [f.name for f in files] == ["init", "roles"]
        assert files[0].label == "0001_init.sql"
        assert len({f.checksum for f in files}) == 2

    def test_ignores_non_sql_files(self, tmp_path: Path) -> None:
        write(tmp_path, "0001_init.sql")
        (tmp_path / "README.md").write_text("abaikan saya\n", encoding="utf-8")
        (tmp_path / "notes.txt").write_text("abaikan saya\n", encoding="utf-8")
        assert len(discover(tmp_path)) == 1

    def test_real_migrations_directory_is_valid(self) -> None:
        files = discover(migrations_dir())
        assert [f.label for f in files] == ["0001_init.sql", "0002_roles.sql"]


class TestNumbering:
    def test_gap_is_rejected(self, tmp_path: Path) -> None:
        write(tmp_path, "0001_init.sql")
        write(tmp_path, "0003_later.sql")
        with pytest.raises(MigrationError, match="celah pada nomor migrasi: 0002"):
            discover(tmp_path)

    def test_multiple_gaps_are_all_reported(self, tmp_path: Path) -> None:
        write(tmp_path, "0001_a.sql")
        write(tmp_path, "0004_d.sql")
        write(tmp_path, "0005_e.sql")
        with pytest.raises(MigrationError, match="0002, 0003"):
            discover(tmp_path)

    def test_duplicate_number_is_rejected(self, tmp_path: Path) -> None:
        write(tmp_path, "0001_init.sql")
        write(tmp_path, "0002_roles.sql")
        write(tmp_path, "0002_roles_again.sql")
        with pytest.raises(MigrationError, match="nomor migrasi ganda: 0002"):
            discover(tmp_path)

    def test_duplicate_message_names_both_files(self, tmp_path: Path) -> None:
        write(tmp_path, "0001_a.sql")
        write(tmp_path, "0001_b.sql")
        with pytest.raises(MigrationError) as exc:
            discover(tmp_path)
        assert "0001_a.sql" in str(exc.value)
        assert "0001_b.sql" in str(exc.value)

    def test_not_starting_at_one_is_rejected(self, tmp_path: Path) -> None:
        write(tmp_path, "0002_init.sql")
        with pytest.raises(MigrationError, match="celah pada nomor migrasi: 0001"):
            discover(tmp_path)

    def test_zero_is_rejected_as_gap(self, tmp_path: Path) -> None:
        write(tmp_path, "0000_init.sql")
        write(tmp_path, "0001_next.sql")
        with pytest.raises(MigrationError, match="harus mulai dari 0001"):
            discover(tmp_path)


class TestFilenames:
    @pytest.mark.parametrize(
        "name",
        [
            "1_init.sql",  # nomor kurang dari empat digit
            "00001_init.sql",  # nomor lebih dari empat digit
            "0001-init.sql",  # pemisah salah
            "0001_Init.sql",  # huruf besar
            "0001_init.SQL",  # ekstensi huruf besar
            "0001_.sql",  # tanpa nama
            "init.sql",  # tanpa nomor
            "0001_init_v2.sql.bak",  # ekstensi ganda
        ],
    )
    def test_bad_filename_is_rejected(self, tmp_path: Path, name: str) -> None:
        write(tmp_path, "0001_ok.sql")
        if name.endswith(".sql"):
            write(tmp_path, name)
            with pytest.raises(MigrationError, match="tidak sesuai pola"):
                discover(tmp_path)
        else:
            write(tmp_path, name)
            assert len(discover(tmp_path)) == 1


class TestTransactionControl:
    def test_begin_in_file_is_rejected(self, tmp_path: Path) -> None:
        write(tmp_path, "0001_init.sql", "BEGIN;\nCREATE TABLE a (id int);\nCOMMIT;\n")
        with pytest.raises(MigrationError, match="kontrol transaksi"):
            discover(tmp_path)

    def test_message_points_at_the_line(self, tmp_path: Path) -> None:
        write(tmp_path, "0001_init.sql", "CREATE TABLE a (id int);\nCOMMIT;\n")
        with pytest.raises(MigrationError, match="baris 2: COMMIT"):
            discover(tmp_path)

    def test_plpgsql_begin_is_allowed(self, tmp_path: Path) -> None:
        write(
            tmp_path,
            "0001_init.sql",
            "CREATE FUNCTION f() RETURNS trigger LANGUAGE plpgsql AS $$\n"
            "BEGIN\n  RAISE EXCEPTION 'no';\nEND;\n$$;\n",
        )
        assert len(discover(tmp_path)) == 1


class TestDirectory:
    def test_missing_directory(self, tmp_path: Path) -> None:
        with pytest.raises(MigrationError, match="direktori migrasi tidak ada"):
            discover(tmp_path / "tidak_ada")

    def test_empty_directory(self, tmp_path: Path) -> None:
        with pytest.raises(MigrationError, match="tidak ada file migrasi"):
            discover(tmp_path)
