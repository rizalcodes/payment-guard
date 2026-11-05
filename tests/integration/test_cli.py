"""Perintah ``guard db ...`` dari ujung ke ujung.

Menjalankan alur yang sesungguhnya dipakai operator:
``bootstrap-roles`` -> ``migrate`` -> ``status``, lewat ``main()``, dengan file
env sementara. ``.env`` dan ``.env.migrator`` milik proyek tidak pernah
disentuh.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

from payment_guard.bootstrap import ENV_KEY_FOR_PROCESS, MIGRATOR_ENV_KEY
from payment_guard.cli import main
from payment_guard.config import (
    ADMIN_URL_ENV_VAR,
    MIGRATOR_ENV_FILENAME,
    MIGRATOR_URL_ENV_VAR,
    RUNTIME_URL_ENV_VARS,
    read_env_file,
)
from payment_guard.migrate import discover, migrations_dir

pytestmark = pytest.mark.integration

#: Nama setiap migrasi di disk, terurut. Diturunkan, bukan ditulis tangan.
#:
#: Tes di bawah memeriksa keluaran CLI yang menyebut **jumlah** migrasi, dan
#: jumlah itu adalah data — berapa berkas yang ada — bukan perilaku. Menuliskan
#: angkanya berarti tes ini gagal setiap kali migrasi bertambah, dengan pesan
#: yang menyalahkan CLI padahal CLI-nya benar. Itu yang terjadi saat
#: ``0003_watcher_from_addr.sql`` ditambahkan di M6.2.
MIGRATION_NAMES = [f.label for f in discover(migrations_dir())]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Jangan biarkan environment nyata bocor ke dalam tes CLI."""
    for name in (*RUNTIME_URL_ENV_VARS.values(), MIGRATOR_URL_ENV_VAR, ADMIN_URL_ENV_VAR):
        monkeypatch.delenv(name, raising=False)


def run(*argv: str) -> int:
    return main(list(argv))


def bootstrap_argv(env_file: Path, migrator_env_file: Path, *extra: str) -> tuple[str, ...]:
    return (
        "db",
        "bootstrap-roles",
        "--env-file",
        str(env_file),
        "--migrator-env-file",
        str(migrator_env_file),
        "--no-prompt",
        *extra,
    )


def migrate_argv(command: str, migrator_env_file: Path) -> tuple[str, ...]:
    return (
        "db",
        command,
        "--migrations-dir",
        str(migrations_dir()),
        "--migrator-env-file",
        str(migrator_env_file),
    )


class TestFullFlow:
    def test_bootstrap_then_migrate_then_status(
        self,
        pristine_db: str,
        env_file: Path,
        migrator_env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(ADMIN_URL_ENV_VAR, pristine_db)
        assert run(*bootstrap_argv(env_file, migrator_env_file)) == 0
        out = capsys.readouterr().out
        assert "role grup dibuat" in out
        assert "guard_rw" in out
        assert "postgresql://" not in out, "URL tidak boleh dicetak"

        # URL admin tidak boleh bocor ke file env mana pun maupun ke stdout.
        assert "devonly_not_a_secret" not in out
        for path in (env_file, migrator_env_file):
            assert "devonly_not_a_secret" not in path.read_text(encoding="utf-8")

        monkeypatch.delenv(ADMIN_URL_ENV_VAR)

        # migrate menemukan URL-nya dari .env.migrator saja, tanpa export manual
        # dan tanpa pernah membuka .env.
        assert run(*migrate_argv("migrate", migrator_env_file)) == 0
        out = capsys.readouterr().out
        # Setiap migrasi disebut namanya, bukan hanya dua yang pertama: itu
        # yang sebenarnya ingin dibuktikan, dan ia tidak pernah basi.
        for name in MIGRATION_NAMES:
            assert name in out, name
        assert f"{len(MIGRATION_NAMES)} migrasi diterapkan" in out

        assert run(*migrate_argv("migrate", migrator_env_file)) == 0
        assert "Tidak ada migrasi baru" in capsys.readouterr().out

        assert run(*migrate_argv("status", migrator_env_file)) == 0
        out = capsys.readouterr().out
        assert out.count("terapan ") >= len(MIGRATION_NAMES)
        assert f"{len(MIGRATION_NAMES)} terapan, 0 tertunda" in out

    def test_runtime_urls_are_usable_after_the_flow(
        self,
        pristine_db: str,
        env_file: Path,
        migrator_env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ADMIN_URL_ENV_VAR, pristine_db)
        assert run(*bootstrap_argv(env_file, migrator_env_file)) == 0
        for var in ENV_KEY_FOR_PROCESS.values():
            url = read_env_file(env_file)[var]
            with psycopg.connect(url) as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                assert cur.fetchone() == (1,)


class TestMigrateNeedsItsOwnFile:
    """``migrate`` dan ``status`` hanya membaca ``.env.migrator``."""

    def test_missing_migrator_file_fails_with_a_clear_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = tmp_path / MIGRATOR_ENV_FILENAME
        assert run(*migrate_argv("migrate", missing)) == 1
        err = capsys.readouterr().err
        assert MIGRATOR_ENV_FILENAME in err
        assert "guard db bootstrap-roles" in err
        assert MIGRATOR_URL_ENV_VAR in err
        assert "postgresql://" not in err

    def test_status_fails_the_same_way(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(*migrate_argv("status", tmp_path / MIGRATOR_ENV_FILENAME)) == 1
        assert MIGRATOR_ENV_FILENAME in capsys.readouterr().err

    def test_migrator_url_in_the_runtime_file_does_not_help(
        self, pristine_db: str, env_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Menempel URL migrator ke .env tidak membuat migrate jalan.

        Pemisahan file hanya berarti kalau tidak ada jalur pintas yang
        membatalkannya.
        """
        env_file.write_text(f"{MIGRATOR_URL_ENV_VAR}={pristine_db}\n", encoding="utf-8")
        assert run(*migrate_argv("migrate", tmp_path / MIGRATOR_ENV_FILENAME)) == 1
        assert MIGRATOR_ENV_FILENAME in capsys.readouterr().err

    def test_migrate_has_no_env_file_option(self) -> None:
        """``--env-file`` sengaja tidak ada di migrate: .env bukan urusannya."""
        with pytest.raises(SystemExit) as exc:
            run("db", "migrate", "--env-file", "/apa/pun")
        assert exc.value.code == 2


class TestRemoteAdminFlag:
    def test_non_loopback_admin_url_is_refused(
        self,
        env_file: Path,
        migrator_env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(
            ADMIN_URL_ENV_VAR, "postgresql://admin:rahasia-admin@10.1.2.3:5434/payment_guard"
        )
        assert run(*bootstrap_argv(env_file, migrator_env_file)) == 1
        err = capsys.readouterr().err
        assert "bukan loopback" in err
        assert "10.1.2.3" in err
        assert "--allow-remote-admin" in err
        assert "rahasia-admin" not in err
        assert "postgresql://" not in err
        assert not env_file.exists()
        assert not migrator_env_file.exists()

    def test_flag_allows_a_loopback_url_through_unchanged(
        self,
        pristine_db: str,
        env_file: Path,
        migrator_env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ADMIN_URL_ENV_VAR, pristine_db)
        assert run(*bootstrap_argv(env_file, migrator_env_file, "--allow-remote-admin")) == 0
        assert MIGRATOR_ENV_KEY in read_env_file(migrator_env_file)


class TestErrorPaths:
    def test_bootstrap_without_admin_url_fails_cleanly(
        self, env_file: Path, migrator_env_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(*bootstrap_argv(env_file, migrator_env_file)) == 1
        assert ADMIN_URL_ENV_VAR in capsys.readouterr().err
        assert not env_file.exists()
        assert not migrator_env_file.exists()

    def test_unreachable_database_message_is_redacted(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(
            MIGRATOR_URL_ENV_VAR,
            "postgresql://seseorang:rahasia-sekali@127.0.0.1:59999/tidakada?connect_timeout=2",
        )
        assert run("db", "status", "--migrations-dir", str(migrations_dir())) == 1
        err = capsys.readouterr().err
        assert "rahasia-sekali" not in err
        assert "seseorang" not in err
        assert "<redacted>" in err
        assert "127.0.0.1:59999" in err


class TestEnvironmentOverride:
    def test_environment_variable_is_accepted_for_ci(
        self, bootstrapped: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """CI bisa menyuntikkan MIGRATOR_DB_URL tanpa menulis file ke disk."""
        monkeypatch.setenv(MIGRATOR_URL_ENV_VAR, bootstrapped[MIGRATOR_ENV_KEY])
        assert run(*migrate_argv("migrate", tmp_path / "tidak-ada-file-ini")) == 0

    def test_cli_does_not_copy_urls_into_the_process_environment(
        self,
        pristine_db: str,
        env_file: Path,
        migrator_env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rahasia tidak disalin ke os.environ, jadi tidak terbawa subproses."""
        monkeypatch.setenv(ADMIN_URL_ENV_VAR, pristine_db)
        assert run(*bootstrap_argv(env_file, migrator_env_file)) == 0
        assert run(*migrate_argv("migrate", migrator_env_file)) == 0
        for var in (*RUNTIME_URL_ENV_VARS.values(), MIGRATOR_URL_ENV_VAR):
            assert var not in os.environ, f"{var} bocor ke environment proses"
