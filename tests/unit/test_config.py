"""Pemisahan ``.env`` dan ``.env.migrator``.

Inti yang dijaga di sini: tidak ada jalur yang membuat URL role pemilik objek
masuk ke konfigurasi runtime. Role itu bisa melepas trigger append-only
(Security v1.1 RR-2), jadi URL-nya tidak boleh bisa diambil dari proses yang
hanya butuh hak runtime.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from payment_guard.config import (
    ADMIN_URL_ENV_VAR,
    MIGRATOR_ENV_FILENAME,
    MIGRATOR_URL_ENV_VAR,
    RUNTIME_ENV_FILENAME,
    RUNTIME_URL_ENV_VARS,
    RuntimeConfig,
    load_runtime_config,
    read_env_file,
    require_migrator_url,
    runtime_config_attributes,
    write_env_file,
)
from payment_guard.db import MigrationError

ROOT = Path(__file__).resolve().parents[2]

MIGRATOR_URL = "postgresql://migrator_owner:rahasia-pemilik@127.0.0.1:5434/payment_guard"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment nyata tidak boleh bocor ke dalam tes konfigurasi."""
    for name in (*RUNTIME_URL_ENV_VARS.values(), MIGRATOR_URL_ENV_VAR, ADMIN_URL_ENV_VAR):
        monkeypatch.delenv(name, raising=False)


def write_runtime(path: Path) -> dict[str, str]:
    values = {
        var: f"postgresql://{p}_app:pw@127.0.0.1:5434/db" for p, var in RUNTIME_URL_ENV_VARS.items()
    }
    write_env_file(path, values)
    return values


class TestFilenames:
    def test_runtime_and_migrator_files_differ(self) -> None:
        assert RUNTIME_ENV_FILENAME == ".env"
        assert MIGRATOR_ENV_FILENAME == ".env.migrator"
        assert RUNTIME_ENV_FILENAME != MIGRATOR_ENV_FILENAME

    def test_migrator_var_is_not_a_runtime_var(self) -> None:
        assert MIGRATOR_URL_ENV_VAR not in RUNTIME_URL_ENV_VARS.values()

    def test_runtime_vars_are_exactly_the_five_processes(self) -> None:
        assert set(RUNTIME_URL_ENV_VARS) == {
            "guard",
            "approver",
            "executor",
            "watcher",
            "reconciler",
        }


class TestRuntimeConfigShape:
    def test_has_no_migrator_attribute(self) -> None:
        """Dites langsung: objek konfigurasi runtime tidak punya slot untuk itu."""
        attributes = runtime_config_attributes()
        assert attributes == (
            "guard_db_url",
            "approver_db_url",
            "executor_db_url",
            "watcher_db_url",
            "reconciler_db_url",
        )
        assert not [a for a in attributes if "migrator" in a]

    def test_attribute_per_runtime_process_and_no_more(self) -> None:
        assert len(runtime_config_attributes()) == len(RUNTIME_URL_ENV_VARS)

    def test_url_for_rejects_migrator(self) -> None:
        config = RuntimeConfig("a", "b", "c", "d", "e")
        with pytest.raises(ValueError, match="proses runtime tidak dikenal"):
            config.url_for("migrator")

    def test_url_for_returns_each_process_url(self) -> None:
        config = RuntimeConfig("g", "a", "e", "w", "r")
        assert config.url_for("guard") == "g"
        assert config.url_for("reconciler") == "r"

    def test_is_frozen(self) -> None:
        config = RuntimeConfig("a", "b", "c", "d", "e")
        with pytest.raises(FrozenInstanceError):
            config.guard_db_url = "lain"  # type: ignore[misc]


class TestRuntimeLoaderIgnoresMigrator:
    def test_migrator_file_is_never_read(self, tmp_path: Path) -> None:
        """Dua file berdampingan; pemuat runtime hanya menyentuh satu."""
        runtime_file = tmp_path / RUNTIME_ENV_FILENAME
        migrator_file = tmp_path / MIGRATOR_ENV_FILENAME
        write_runtime(runtime_file)
        write_env_file(migrator_file, {MIGRATOR_URL_ENV_VAR: MIGRATOR_URL})

        config = load_runtime_config(runtime_file)
        loaded = [getattr(config, name) for name in runtime_config_attributes()]
        assert MIGRATOR_URL not in loaded
        assert not [url for url in loaded if "migrator_owner" in url]
        assert not [url for url in loaded if "rahasia-pemilik" in url]

    def test_migrator_url_in_runtime_file_is_still_not_exposed(self, tmp_path: Path) -> None:
        """Bahkan bila operator salah menempel URL migrator ke .env.

        RuntimeConfig tidak punya tempat untuk menyimpannya, jadi tidak ada
        atribut yang bisa dibaca proses runtime.
        """
        runtime_file = tmp_path / RUNTIME_ENV_FILENAME
        write_runtime(runtime_file)
        write_env_file(runtime_file, {MIGRATOR_URL_ENV_VAR: MIGRATOR_URL})

        config = load_runtime_config(runtime_file)
        loaded = [getattr(config, name) for name in runtime_config_attributes()]
        assert MIGRATOR_URL not in loaded

    def test_environment_wins_over_file(self, tmp_path: Path, monkeypatch) -> None:
        runtime_file = tmp_path / RUNTIME_ENV_FILENAME
        write_runtime(runtime_file)
        monkeypatch.setenv("GUARD_DB_URL", "dari-environment")
        assert load_runtime_config(runtime_file).guard_db_url == "dari-environment"

    def test_does_not_mutate_process_environment(self, tmp_path: Path) -> None:
        """Rahasia tidak menyebar ke environment, jadi tidak ikut ke subproses."""
        import os

        runtime_file = tmp_path / RUNTIME_ENV_FILENAME
        write_runtime(runtime_file)
        load_runtime_config(runtime_file)
        for var in RUNTIME_URL_ENV_VARS.values():
            assert var not in os.environ

    def test_missing_urls_fail_loudly(self, tmp_path: Path) -> None:
        with pytest.raises(MigrationError) as exc:
            load_runtime_config(tmp_path / RUNTIME_ENV_FILENAME)
        message = str(exc.value)
        assert "GUARD_DB_URL" in message
        assert "bootstrap-roles" in message

    def test_require_false_allows_empty(self, tmp_path: Path) -> None:
        config = load_runtime_config(tmp_path / RUNTIME_ENV_FILENAME, require=False)
        assert config.guard_db_url == ""


class TestRequireMigratorUrl:
    def test_reads_from_migrator_file(self, tmp_path: Path) -> None:
        path = tmp_path / MIGRATOR_ENV_FILENAME
        write_env_file(path, {MIGRATOR_URL_ENV_VAR: MIGRATOR_URL})
        assert require_migrator_url(path) == MIGRATOR_URL

    def test_never_reads_the_runtime_file(self, tmp_path: Path) -> None:
        """URL migrator di .env tidak boleh menolong: filenya memang salah."""
        write_env_file(tmp_path / RUNTIME_ENV_FILENAME, {MIGRATOR_URL_ENV_VAR: MIGRATOR_URL})
        with pytest.raises(MigrationError):
            require_migrator_url(tmp_path / MIGRATOR_ENV_FILENAME)

    def test_missing_file_message_names_file_and_command(self, tmp_path: Path) -> None:
        path = tmp_path / MIGRATOR_ENV_FILENAME
        with pytest.raises(MigrationError) as exc:
            require_migrator_url(path)
        message = str(exc.value)
        assert MIGRATOR_ENV_FILENAME in message
        assert "guard db bootstrap-roles" in message
        assert MIGRATOR_URL_ENV_VAR in message

    def test_missing_file_message_has_no_secret(self, tmp_path: Path) -> None:
        with pytest.raises(MigrationError) as exc:
            require_migrator_url(tmp_path / MIGRATOR_ENV_FILENAME)
        assert "postgresql://" not in str(exc.value)

    def test_empty_value_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / MIGRATOR_ENV_FILENAME
        path.write_text(f"{MIGRATOR_URL_ENV_VAR}=\n", encoding="utf-8")
        with pytest.raises(MigrationError, match="kosong"):
            require_migrator_url(path)

    def test_environment_wins_over_file(self, tmp_path: Path, monkeypatch) -> None:
        """Supaya CI bisa menyuntikkan URL tanpa menulis file ke disk."""
        path = tmp_path / MIGRATOR_ENV_FILENAME
        write_env_file(path, {MIGRATOR_URL_ENV_VAR: MIGRATOR_URL})
        monkeypatch.setenv(MIGRATOR_URL_ENV_VAR, "dari-environment")
        assert require_migrator_url(path) == "dari-environment"


class TestExampleFiles:
    def test_runtime_example_has_no_migrator_url(self) -> None:
        assert MIGRATOR_URL_ENV_VAR not in read_env_file(ROOT / ".env.example")

    def test_runtime_example_lists_all_five_runtime_urls(self) -> None:
        keys = set(read_env_file(ROOT / ".env.example"))
        assert set(RUNTIME_URL_ENV_VARS.values()) <= keys

    def test_migrator_example_exists_and_is_empty(self) -> None:
        values = read_env_file(ROOT / ".env.migrator.example")
        assert values == {MIGRATOR_URL_ENV_VAR: ""}

    def test_neither_example_mentions_the_admin_variable(self) -> None:
        """Security v1.1 I6: URL admin tidak pernah masuk file env."""
        for name in (".env.example", ".env.migrator.example"):
            assert ADMIN_URL_ENV_VAR not in read_env_file(ROOT / name)
