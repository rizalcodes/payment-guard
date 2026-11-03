"""Runner migrasi terhadap Postgres 16 sungguhan.

Menutup daftar tes wajib M0: database kosong, dijalankan dua kali, checksum
berubah, file hilang, celah/nomor ganda, rollback penuh bila gagal di tengah,
multi-statement dengan ``$$``, dan notice "no privileges" yang menggagalkan
migrasi (ST-41).
"""

from __future__ import annotations

import threading
from pathlib import Path

import psycopg
import pytest

from payment_guard.bootstrap import MIGRATOR_ENV_KEY
from payment_guard.db import MigrationError
from payment_guard.locks import MIGRATE_LOCK_KEY
from payment_guard.migrate import migrations_dir, run_migrate, run_status
from payment_guard.privileges import OWNER_ONLY_TABLES, TABLE_MATRIX

pytestmark = pytest.mark.integration

#: Semua tabel yang harus ada setelah 0001 dan 0002 diterapkan.
EXPECTED_TABLES = frozenset(TABLE_MATRIX) | OWNER_ONLY_TABLES

REAL_MIGRATIONS = migrations_dir()


def write_migration(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


def table_names(url: str) -> set[str]:
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
        )
        return {row[0] for row in cur.fetchall()}


def scalar(url: str, query: str, params: tuple[object, ...] = ()) -> object:
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
        return row[0] if row else None


class TestEmptyDatabase:
    def test_all_tables_are_created(self, bootstrapped: dict[str, str]) -> None:
        url = bootstrapped[MIGRATOR_ENV_KEY]
        applied = run_migrate(url, REAL_MIGRATIONS)
        assert [f.label for f in applied] == ["0001_init.sql", "0002_roles.sql"]
        assert EXPECTED_TABLES <= table_names(url)

    def test_no_unexpected_tables(self, migrated: dict[str, str]) -> None:
        """Dua arah: database tidak boleh punya tabel di luar yang didaftar."""
        assert table_names(migrated[MIGRATOR_ENV_KEY]) == EXPECTED_TABLES

    def test_triggers_and_function_from_0001_exist(self, migrated: dict[str, str]) -> None:
        url = migrated[MIGRATOR_ENV_KEY]
        assert scalar(url, "SELECT to_regproc('append_only_guard') IS NOT NULL") is True
        triggers = scalar(
            url,
            "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal "
            "AND tgrelid IN ('audit_log'::regclass, 'mandates'::regclass)",
        )
        assert triggers == 4

    def test_schema_migrations_records_name_and_checksum(self, migrated: dict[str, str]) -> None:
        url = migrated[MIGRATOR_ENV_KEY]
        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version")
            rows = cur.fetchall()
        assert [(r[0], r[1]) for r in rows] == [(1, "init"), (2, "roles")]
        for _, _, checksum in rows:
            assert len(checksum) == 64


class TestIdempotence:
    def test_second_run_does_nothing(self, bootstrapped: dict[str, str]) -> None:
        url = bootstrapped[MIGRATOR_ENV_KEY]
        first = run_migrate(url, REAL_MIGRATIONS)
        assert len(first) == 2
        second = run_migrate(url, REAL_MIGRATIONS)
        assert second == []

    def test_second_run_does_not_change_applied_at(self, bootstrapped: dict[str, str]) -> None:
        url = bootstrapped[MIGRATOR_ENV_KEY]
        run_migrate(url, REAL_MIGRATIONS)
        before = scalar(url, "SELECT max(applied_at) FROM schema_migrations")
        run_migrate(url, REAL_MIGRATIONS)
        assert scalar(url, "SELECT max(applied_at) FROM schema_migrations") == before

    def test_third_run_still_does_nothing(self, migrated: dict[str, str]) -> None:
        url = migrated[MIGRATOR_ENV_KEY]
        assert run_migrate(url, REAL_MIGRATIONS) == []
        assert run_migrate(url, REAL_MIGRATIONS) == []


class TestChecksumEnforcement:
    @pytest.fixture
    def staged(self, bootstrapped: dict[str, str], tmp_path: Path) -> tuple[str, Path]:
        """Satu migrasi yang sudah terapan, di direktori yang bisa diedit tes."""
        directory = tmp_path / "migrations"
        directory.mkdir()
        write_migration(directory, "0001_init.sql", "CREATE TABLE a (id int);\n")
        url = bootstrapped[MIGRATOR_ENV_KEY]
        assert len(run_migrate(url, directory)) == 1
        return url, directory

    def test_edited_applied_file_is_rejected(self, staged: tuple[str, Path]) -> None:
        url, directory = staged
        write_migration(directory, "0001_init.sql", "CREATE TABLE a (id int, extra int);\n")
        with pytest.raises(MigrationError) as exc:
            run_migrate(url, directory)
        message = str(exc.value)
        assert "checksum 0001_init.sql berubah" in message
        assert "di database" in message and "di disk" in message

    def test_whitespace_only_edit_is_still_rejected(self, staged: tuple[str, Path]) -> None:
        """Checksum atas isi, bukan atas makna: spasi tambahan juga ditolak."""
        url, directory = staged
        write_migration(directory, "0001_init.sql", "CREATE TABLE  a (id int);\n")
        with pytest.raises(MigrationError, match="checksum"):
            run_migrate(url, directory)

    def test_line_ending_change_is_accepted(self, staged: tuple[str, Path]) -> None:
        """CRLF ke LF tidak mengubah checksum: itu gunanya normalisasi."""
        url, directory = staged
        (directory / "0001_init.sql").write_bytes(b"CREATE TABLE a (id int);\r\n")
        assert run_migrate(url, directory) == []

    def test_rename_of_applied_file_is_rejected(self, staged: tuple[str, Path]) -> None:
        url, directory = staged
        (directory / "0001_init.sql").rename(directory / "0001_renamed.sql")
        with pytest.raises(MigrationError, match="berganti nama"):
            run_migrate(url, directory)

    def test_missing_applied_file_is_rejected(self, staged: tuple[str, Path]) -> None:
        """File terapan yang dihapus harus ditolak, bukan diabaikan.

        Penomoran di disk tetap rapat (hanya 0001 yang tersisa), jadi yang
        tertangkap di sini benar-benar pemeriksaan "file terapan hilang" dan
        bukan pemeriksaan celah nomor.
        """
        url, directory = staged
        write_migration(directory, "0002_next.sql", "CREATE TABLE b (id int);\n")
        assert len(run_migrate(url, directory)) == 1

        (directory / "0002_next.sql").unlink()
        with pytest.raises(MigrationError) as exc:
            run_migrate(url, directory)
        assert "tidak ada lagi di disk" in str(exc.value)
        assert "0002_next.sql" in str(exc.value)
        assert "forward-only" in str(exc.value)

    def test_gap_is_rejected_before_touching_database(self, staged: tuple[str, Path]) -> None:
        url, directory = staged
        write_migration(directory, "0003_later.sql", "CREATE TABLE c (id int);\n")
        with pytest.raises(MigrationError, match="celah pada nomor migrasi"):
            run_migrate(url, directory)
        # Tidak ada yang diterapkan diam-diam.
        assert scalar(url, "SELECT count(*) FROM schema_migrations") == 1
        assert "c" not in table_names(url)

    def test_duplicate_number_is_rejected(self, staged: tuple[str, Path]) -> None:
        url, directory = staged
        write_migration(directory, "0002_a.sql", "CREATE TABLE b (id int);\n")
        write_migration(directory, "0002_b.sql", "CREATE TABLE c (id int);\n")
        with pytest.raises(MigrationError, match="nomor migrasi ganda"):
            run_migrate(url, directory)
        assert scalar(url, "SELECT count(*) FROM schema_migrations") == 1

    def test_inserting_below_highest_applied_is_rejected(
        self, bootstrapped: dict[str, str], tmp_path: Path
    ) -> None:
        directory = tmp_path / "migrations"
        directory.mkdir()
        write_migration(directory, "0001_a.sql", "CREATE TABLE a (id int);\n")
        write_migration(directory, "0002_b.sql", "CREATE TABLE b (id int);\n")
        url = bootstrapped[MIGRATOR_ENV_KEY]
        run_migrate(url, directory)

        # 0002 dihapus dari database seolah operator "memundurkan" satu langkah,
        # lalu file baru disisipkan. Forward-only harus menolaknya.
        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM schema_migrations WHERE version = 1")
            conn.commit()
        with pytest.raises(MigrationError, match="forward-only"):
            run_migrate(url, directory)


class TestAtomicity:
    def test_error_in_the_middle_rolls_back_the_whole_file(
        self, bootstrapped: dict[str, str], tmp_path: Path
    ) -> None:
        directory = tmp_path / "migrations"
        directory.mkdir()
        write_migration(
            directory,
            "0001_init.sql",
            "CREATE TABLE sebelum_error (id int);\n"
            "CREATE TABLE juga_sebelum (id int);\n"
            "CREATE TABLE sebelum_error (id int);\n"  # sengaja bentrok
            "CREATE TABLE sesudah_error (id int);\n",
        )
        url = bootstrapped[MIGRATOR_ENV_KEY]
        with pytest.raises(MigrationError) as exc:
            run_migrate(url, directory)
        assert "0001_init.sql gagal" in str(exc.value)
        assert "Transaksi dibatalkan" in str(exc.value)

        # Tidak ada setengah skema.
        names = table_names(url)
        assert "sebelum_error" not in names
        assert "juga_sebelum" not in names
        assert "sesudah_error" not in names
        assert scalar(url, "SELECT count(*) FROM schema_migrations") == 0

    def test_failure_in_second_file_keeps_the_first(
        self, bootstrapped: dict[str, str], tmp_path: Path
    ) -> None:
        directory = tmp_path / "migrations"
        directory.mkdir()
        write_migration(directory, "0001_ok.sql", "CREATE TABLE baik (id int);\n")
        write_migration(directory, "0002_bad.sql", "CREATE TABLE buruk (id int);\nSELECT 1/0;\n")
        url = bootstrapped[MIGRATOR_ENV_KEY]
        with pytest.raises(MigrationError, match=r"0002_bad\.sql gagal"):
            run_migrate(url, directory)

        names = table_names(url)
        assert "baik" in names
        assert "buruk" not in names
        assert scalar(url, "SELECT count(*) FROM schema_migrations") == 1

        # Dan setelah file rusak diperbaiki, migrasi bisa dilanjutkan.
        write_migration(directory, "0002_bad.sql", "CREATE TABLE sudah_benar (id int);\n")
        assert [f.label for f in run_migrate(url, directory)] == ["0002_bad.sql"]
        assert "sudah_benar" in table_names(url)

    def test_error_message_carries_postgres_detail(
        self, bootstrapped: dict[str, str], tmp_path: Path
    ) -> None:
        directory = tmp_path / "migrations"
        directory.mkdir()
        write_migration(directory, "0001_init.sql", "CREATE TABLE a (id int, id int);\n")
        with pytest.raises(MigrationError) as exc:
            run_migrate(bootstrapped[MIGRATOR_ENV_KEY], directory)
        assert 'column "id" specified more than once' in str(exc.value)


class TestMultiStatementAndDollarQuoting:
    def test_whole_file_runs_including_dollar_quoted_bodies(
        self, bootstrapped: dict[str, str], tmp_path: Path
    ) -> None:
        """Runner tidak memecah titik koma, jadi $$ ... $$ tetap utuh."""
        directory = tmp_path / "migrations"
        directory.mkdir()
        write_migration(
            directory,
            "0001_init.sql",
            """
CREATE TABLE barang (id int PRIMARY KEY, nama text);

CREATE FUNCTION tambah_satu(n int) RETURNS int
LANGUAGE plpgsql AS $$
BEGIN
  -- Titik koma di dalam badan fungsi: pemecah naif akan merusak ini.
  RETURN n + 1;
END;
$$;

CREATE FUNCTION tolak_perubahan() RETURNS trigger
LANGUAGE plpgsql AS $body$
BEGIN
  RAISE EXCEPTION '% ditolak; tabel ini append-only', TG_OP;
END;
$body$;

CREATE TRIGGER barang_append_only
  BEFORE UPDATE OR DELETE ON barang
  FOR EACH ROW EXECUTE FUNCTION tolak_perubahan();

DO $do$
BEGIN
  INSERT INTO barang (id, nama) VALUES (1, 'satu');
  INSERT INTO barang (id, nama) VALUES (2, 'dua');
END
$do$;
""",
        )
        url = bootstrapped[MIGRATOR_ENV_KEY]
        assert len(run_migrate(url, directory)) == 1

        assert scalar(url, "SELECT tambah_satu(41)") == 42
        assert scalar(url, "SELECT count(*) FROM barang") == 2

        # Trigger dari blok $$ benar-benar aktif.
        with psycopg.connect(url) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                cur.execute("UPDATE barang SET nama = 'x' WHERE id = 1")
            conn.rollback()

    def test_semicolon_inside_string_literal_survives(
        self, bootstrapped: dict[str, str], tmp_path: Path
    ) -> None:
        directory = tmp_path / "migrations"
        directory.mkdir()
        write_migration(
            directory,
            "0001_init.sql",
            "CREATE TABLE t (id int, catatan text);\nINSERT INTO t VALUES (1, 'a; b; c');\n",
        )
        url = bootstrapped[MIGRATOR_ENV_KEY]
        run_migrate(url, directory)
        assert scalar(url, "SELECT catatan FROM t WHERE id = 1") == "a; b; c"


class TestPrivilegeNotices:
    """ST-41: hak akses yang diam-diam gagal harus menggagalkan migrasi."""

    def test_failed_revoke_fails_the_migration(
        self, bootstrapped: dict[str, str], tmp_path: Path
    ) -> None:
        """Kasus E11 persis: REVOKE yang tidak berefek apa pun.

        Migrator bukan pemilik schema ``public``, jadi REVOKE di tingkat schema
        tidak melakukan apa-apa dan Postgres hanya memancarkan WARNING. Tanpa
        notice handler, migrasi akan tampak sukses padahal hak PUBLIC masih
        terpasang: tepat kegagalan diam-diam yang harus dicegah.
        """
        directory = tmp_path / "migrations"
        directory.mkdir()
        write_migration(directory, "0001_revoke.sql", "REVOKE ALL ON SCHEMA public FROM PUBLIC;\n")
        url = bootstrapped[MIGRATOR_ENV_KEY]

        with pytest.raises(MigrationError) as exc:
            run_migrate(url, directory)
        message = str(exc.value)
        assert "no privileges could be revoked" in message
        assert "Security v1.1 E11" in message
        assert scalar(url, "SELECT count(*) FROM schema_migrations") == 0

    def test_failed_grant_fails_the_migration(
        self, bootstrapped: dict[str, str], pristine_db: str, tmp_path: Path
    ) -> None:
        """GRANT tanpa grant option: berefek nol, hanya WARNING.

        Migrator diberi SELECT (tanpa GRANT OPTION) atas tabel milik admin.
        Dengan hak itu, GRANT-nya tidak error melainkan lolos dengan WARNING,
        yang merupakan mode kegagalan yang berbahaya.
        """
        with psycopg.connect(pristine_db, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("CREATE TABLE milik_admin (id int)")
            cur.execute("GRANT SELECT ON milik_admin TO migrator_owner")

        directory = tmp_path / "migrations"
        directory.mkdir()
        write_migration(directory, "0001_grants.sql", "GRANT SELECT ON milik_admin TO guard_rw;\n")

        url = bootstrapped[MIGRATOR_ENV_KEY]
        with pytest.raises(MigrationError) as exc:
            run_migrate(url, directory)
        message = str(exc.value)
        assert "no privileges were granted" in message
        assert scalar(url, "SELECT count(*) FROM schema_migrations") == 0
        # Dan hak itu benar-benar tidak terpasang.
        assert (
            scalar(pristine_db, "SELECT has_table_privilege('guard_rw', 'milik_admin', 'SELECT')")
            is False
        )
        assert "notice hak akses" in message

    def test_ordinary_notices_do_not_fail_the_migration(
        self, bootstrapped: dict[str, str], tmp_path: Path
    ) -> None:
        """Hanya pola "no privileges" yang fatal; NOTICE lain dibiarkan."""
        directory = tmp_path / "migrations"
        directory.mkdir()
        write_migration(
            directory,
            "0001_init.sql",
            "CREATE TABLE a (id int PRIMARY KEY);\n"  # memancarkan NOTICE indeks implisit
            "DO $$ BEGIN RAISE NOTICE 'sekadar catatan'; END $$;\n",
        )
        url = bootstrapped[MIGRATOR_ENV_KEY]
        assert len(run_migrate(url, directory)) == 1
        assert "a" in table_names(url)


class TestRolesMigrationGuard:
    """ST-41: 0002_roles.sql harus gagal keras bila role grup belum ada."""

    def test_0002_fails_hard_without_group_roles(self, pristine_roles: str) -> None:
        # Dijalankan sebagai admin, bukan migrator: pada titik ini bootstrap
        # belum jalan, jadi role migrator pun belum ada. Yang diuji di sini
        # adalah penjaga di dalam file SQL, bukan identitas pemanggilnya.
        with pytest.raises(MigrationError) as exc:
            run_migrate(pristine_roles, REAL_MIGRATIONS)
        message = str(exc.value)
        assert "jalankan guard db bootstrap-roles dulu" in message
        assert "guard_rw" in message

        # 0001 tetap terapan; 0002 dibatalkan penuh.
        assert scalar(pristine_roles, "SELECT count(*) FROM schema_migrations") == 1
        assert "audit_log" in table_names(pristine_roles)

    def test_0002_succeeds_after_bootstrap(self, bootstrapped: dict[str, str]) -> None:
        applied = run_migrate(bootstrapped[MIGRATOR_ENV_KEY], REAL_MIGRATIONS)
        assert [f.label for f in applied] == ["0001_init.sql", "0002_roles.sql"]


class TestAdvisoryLock:
    def test_runner_waits_for_the_migrate_lock(self, bootstrapped: dict[str, str]) -> None:
        """Dua runner tidak boleh jalan bersamaan.

        Lock dipegang di koneksi lain pada database yang sama; runner harus
        menunggu, lalu selesai setelah lock dilepas.
        """
        url = bootstrapped[MIGRATOR_ENV_KEY]
        outcome: dict[str, object] = {}

        def worker() -> None:
            try:
                outcome["applied"] = [f.label for f in run_migrate(url, REAL_MIGRATIONS)]
            # Apa pun yang terjadi di thread dilaporkan ke tes utama.
            except BaseException as exc:
                outcome["error"] = exc

        with psycopg.connect(url, autocommit=True) as blocker, blocker.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (MIGRATE_LOCK_KEY,))

            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            thread.join(timeout=2.0)
            assert thread.is_alive(), "runner tidak menunggu lock migrate"
            assert outcome == {}

            cur.execute("SELECT pg_advisory_unlock(%s)", (MIGRATE_LOCK_KEY,))
            thread.join(timeout=30)

        assert not thread.is_alive()
        assert outcome.get("error") is None, outcome.get("error")
        assert outcome["applied"] == ["0001_init.sql", "0002_roles.sql"]

    def test_lock_key_matches_the_specification(self) -> None:
        from payment_guard.locks import advisory_key

        assert MIGRATE_LOCK_KEY == advisory_key("migrate", "")


class TestStatus:
    def test_status_before_any_migration(self, bootstrapped: dict[str, str]) -> None:
        report = run_status(bootstrapped[MIGRATOR_ENV_KEY], REAL_MIGRATIONS)
        assert report.table_exists is False
        assert report.applied == []
        assert [f.label for f in report.pending] == ["0001_init.sql", "0002_roles.sql"]

    def test_status_after_full_migration(self, migrated: dict[str, str]) -> None:
        report = run_status(migrated[MIGRATOR_ENV_KEY], REAL_MIGRATIONS)
        assert report.table_exists is True
        assert [row.version for row in report.applied] == [1, 2]
        assert report.pending == []

    def test_status_detects_checksum_drift(
        self, bootstrapped: dict[str, str], tmp_path: Path
    ) -> None:
        directory = tmp_path / "migrations"
        directory.mkdir()
        write_migration(directory, "0001_init.sql", "CREATE TABLE a (id int);\n")
        url = bootstrapped[MIGRATOR_ENV_KEY]
        run_migrate(url, directory)
        write_migration(directory, "0001_init.sql", "CREATE TABLE a (id bigint);\n")
        with pytest.raises(MigrationError, match="checksum"):
            run_status(url, directory)

    def test_status_does_not_apply_anything(self, bootstrapped: dict[str, str]) -> None:
        url = bootstrapped[MIGRATOR_ENV_KEY]
        run_status(url, REAL_MIGRATIONS)
        assert "audit_log" not in table_names(url)
