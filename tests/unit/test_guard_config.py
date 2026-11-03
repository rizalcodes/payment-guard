"""Konfigurasi Guard: path kunci di luar repo, batas, dan isolasi ``.env``.

Dua sifat yang dijaga paling keras di sini:

* ``GUARD_SIGNING_KEY_PATH`` **harus** di luar akar repo. Berkas kunci di
  dalam repo bisa ikut ter-commit karena satu ``git add -f`` atau satu aturan
  ``.gitignore`` yang berubah; menaruhnya di luar menghapus seluruh kelas
  kesalahan itu (Security v1.1 I1, asumsi 2).
* Pemuat ini **tidak pernah** membuka ``.env.migrator``. Dijaga dengan spy
  pada ``read_env_file``, bukan hanya dengan membaca kodenya.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from payment_guard import config as config_module
from payment_guard.config import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_LOCK_TIMEOUT_MS,
    GUARD_ENV_VARS,
    HEARTBEAT_SECONDS_MAX,
    HEARTBEAT_SECONDS_MIN,
    MIGRATOR_ENV_FILENAME,
    RUNTIME_ENV_FILENAME,
    GuardConfig,
    GuardConfigError,
    load_guard_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in GUARD_ENV_VARS.values():
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    """Akar repo tiruan, dengan berkas kunci di luar dan di dalamnya."""
    repo = tmp_path / "repo"
    (repo / "keys").mkdir(parents=True)
    outside = tmp_path / "secrets"
    outside.mkdir()

    inside_key = repo / "keys" / "guard_signing.json"
    inside_key.write_text("{}", encoding="utf-8")
    outside_key = outside / "guard_signing.json"
    outside_key.write_text("{}", encoding="utf-8")

    mandate = repo / "mandate.json"
    mandate.write_text("{}", encoding="utf-8")
    public_keys = repo / "guard_public.json"
    public_keys.write_text("{}", encoding="utf-8")

    env_file = repo / RUNTIME_ENV_FILENAME
    env_file.write_text("", encoding="utf-8")

    return {
        "repo": repo,
        "env_file": env_file,
        "mandate": mandate,
        "public_keys": public_keys,
        "inside_key": inside_key,
        "outside_key": outside_key,
    }


def write_env(paths: dict[str, Path], **overrides: str) -> None:
    values = {
        "GUARD_DB_URL": "postgresql://guard_app:pw@127.0.0.1:5434/payment_guard",
        "GUARD_MANDATE_PATH": str(paths["mandate"]),
        "GUARD_SIGNING_KEY_PATH": str(paths["outside_key"]),
        "GUARD_PUBLIC_KEYS_PATH": str(paths["public_keys"]),
    }
    values.update(overrides)
    lines = [f"{k}={v}" for k, v in values.items() if v != ""]
    paths["env_file"].write_text("\n".join(lines) + "\n", encoding="utf-8")


def load(paths: dict[str, Path]) -> GuardConfig:
    return load_guard_config(paths["env_file"], repo_root=paths["repo"])


class TestKeyPathMustBeOutsideRepo:
    def test_outside_repo_accepted(self, workspace: dict[str, Path]) -> None:
        write_env(workspace)
        assert load(workspace).signing_key_path == workspace["outside_key"].resolve()

    def test_inside_repo_rejected(self, workspace: dict[str, Path]) -> None:
        write_env(workspace, GUARD_SIGNING_KEY_PATH=str(workspace["inside_key"]))
        with pytest.raises(GuardConfigError, match="di dalam akar repo"):
            load(workspace)

    def test_rejection_names_both_paths(self, workspace: dict[str, Path]) -> None:
        write_env(workspace, GUARD_SIGNING_KEY_PATH=str(workspace["inside_key"]))
        with pytest.raises(GuardConfigError) as exc:
            load(workspace)
        message = str(exc.value)
        assert str(workspace["inside_key"].resolve()) in message
        assert str(workspace["repo"].resolve()) in message

    def test_rejection_explains_why(self, workspace: dict[str, Path]) -> None:
        write_env(workspace, GUARD_SIGNING_KEY_PATH=str(workspace["inside_key"]))
        with pytest.raises(GuardConfigError) as exc:
            load(workspace)
        assert "ter-commit" in str(exc.value)

    def test_repo_root_itself_rejected(self, workspace: dict[str, Path]) -> None:
        """Path yang persis akar repo juga ditolak, bukan hanya yang di dalamnya."""
        write_env(workspace, GUARD_SIGNING_KEY_PATH=str(workspace["repo"]))
        with pytest.raises(GuardConfigError):
            load(workspace)

    def test_relative_path_pointing_inside_is_rejected(
        self, workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pemeriksaannya atas path yang sudah di-resolve, bukan atas teksnya."""
        monkeypatch.chdir(workspace["repo"])
        write_env(workspace, GUARD_SIGNING_KEY_PATH="keys/guard_signing.json")
        with pytest.raises(GuardConfigError, match="di dalam akar repo"):
            load(workspace)

    def test_dotdot_path_escaping_the_repo_is_accepted(
        self, workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(workspace["repo"])
        write_env(workspace, GUARD_SIGNING_KEY_PATH="../secrets/guard_signing.json")
        assert load(workspace).signing_key_path == workspace["outside_key"].resolve()

    def test_public_keys_inside_repo_is_fine(self, workspace: dict[str, Path]) -> None:
        """guard_public.json memang boleh di-commit (Arsitektur v1.1 2.1)."""
        write_env(workspace)
        assert load(workspace).public_keys_path == workspace["public_keys"].resolve()


class TestFileExistence:
    @pytest.mark.parametrize(
        ("var", "field"),
        [
            ("GUARD_MANDATE_PATH", "mandate_path"),
            ("GUARD_SIGNING_KEY_PATH", "signing_key_path"),
            ("GUARD_PUBLIC_KEYS_PATH", "public_keys_path"),
        ],
    )
    def test_missing_file_rejected(self, workspace: dict[str, Path], var: str, field: str) -> None:
        write_env(workspace, **{var: str(workspace["repo"].parent / "tidak-ada.json")})
        with pytest.raises(GuardConfigError, match="tidak ada"):
            load(workspace)

    def test_directory_instead_of_file_rejected(self, workspace: dict[str, Path]) -> None:
        write_env(workspace, GUARD_MANDATE_PATH=str(workspace["repo"]))
        with pytest.raises(GuardConfigError, match="harus menunjuk berkas"):
            load(workspace)

    @pytest.mark.parametrize("var", sorted(GUARD_ENV_VARS.values()))
    def test_each_required_variable_is_checked(self, workspace: dict[str, Path], var: str) -> None:
        """Yang punya default boleh kosong; yang tidak, harus ditolak."""
        write_env(workspace, **{var: ""})
        optional = {"GUARD_HEARTBEAT_SECONDS", "GUARD_LOCK_TIMEOUT_MS"}
        if var in optional:
            load(workspace)
        else:
            with pytest.raises(GuardConfigError):
                load(workspace)

    def test_missing_db_url_message_names_bootstrap(self, workspace: dict[str, Path]) -> None:
        write_env(workspace, GUARD_DB_URL="")
        with pytest.raises(GuardConfigError) as exc:
            load(workspace)
        assert "bootstrap-roles" in str(exc.value)


class TestBounds:
    def test_defaults(self, workspace: dict[str, Path]) -> None:
        write_env(workspace)
        loaded = load(workspace)
        assert loaded.heartbeat_seconds == DEFAULT_HEARTBEAT_SECONDS
        assert loaded.lock_timeout_ms == DEFAULT_LOCK_TIMEOUT_MS

    @pytest.mark.parametrize("value", [HEARTBEAT_SECONDS_MIN, 5, 30, HEARTBEAT_SECONDS_MAX])
    def test_heartbeat_inside_bounds_accepted(self, workspace: dict[str, Path], value: int) -> None:
        write_env(workspace, GUARD_HEARTBEAT_SECONDS=str(value))
        assert load(workspace).heartbeat_seconds == value

    @pytest.mark.parametrize(
        "value",
        [
            HEARTBEAT_SECONDS_MIN - 1,
            0,
            -5,
            HEARTBEAT_SECONDS_MAX + 1,
            3600,
        ],
    )
    def test_heartbeat_outside_bounds_rejected(
        self, workspace: dict[str, Path], value: int
    ) -> None:
        write_env(workspace, GUARD_HEARTBEAT_SECONDS=str(value))
        with pytest.raises(GuardConfigError, match=r"harus 1\.\.60"):
            load(workspace)

    @pytest.mark.parametrize("value", ["lima", "5.0", "5s", " "])
    def test_non_integer_heartbeat_rejected(self, workspace: dict[str, Path], value: str) -> None:
        write_env(workspace, GUARD_HEARTBEAT_SECONDS=value)
        if value.strip() == "":
            assert load(workspace).heartbeat_seconds == DEFAULT_HEARTBEAT_SECONDS
        else:
            with pytest.raises(GuardConfigError, match="bilangan bulat"):
                load(workspace)

    def test_lock_timeout_bounds(self, workspace: dict[str, Path]) -> None:
        write_env(workspace, GUARD_LOCK_TIMEOUT_MS="100")
        assert load(workspace).lock_timeout_ms == 100
        write_env(workspace, GUARD_LOCK_TIMEOUT_MS="0")
        with pytest.raises(GuardConfigError):
            load(workspace)


class TestPrecedence:
    def test_environment_wins_over_file(
        self, workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_env(workspace, GUARD_HEARTBEAT_SECONDS="7")
        monkeypatch.setenv("GUARD_HEARTBEAT_SECONDS", "11")
        assert load(workspace).heartbeat_seconds == 11

    def test_environment_alone_is_enough(
        self, workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace["env_file"].write_text("", encoding="utf-8")
        monkeypatch.setenv("GUARD_DB_URL", "postgresql://a:b@127.0.0.1:5434/d")
        monkeypatch.setenv("GUARD_MANDATE_PATH", str(workspace["mandate"]))
        monkeypatch.setenv("GUARD_SIGNING_KEY_PATH", str(workspace["outside_key"]))
        monkeypatch.setenv("GUARD_PUBLIC_KEYS_PATH", str(workspace["public_keys"]))
        assert load(workspace).heartbeat_seconds == DEFAULT_HEARTBEAT_SECONDS


class TestNeverOpensMigratorFile:
    def test_only_the_runtime_env_file_is_read(
        self, workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spy pada read_env_file: tidak ada path ``.env.migrator`` yang dibaca."""
        seen: list[Path] = []
        real = config_module.read_env_file

        def spy(path: Path) -> dict[str, str]:
            seen.append(path)
            return real(path)

        monkeypatch.setattr(config_module, "read_env_file", spy)
        write_env(workspace)
        load(workspace)

        assert seen == [workspace["env_file"]]
        assert not [p for p in seen if p.name == MIGRATOR_ENV_FILENAME]

    def test_a_migrator_file_sitting_next_to_it_is_ignored(
        self, workspace: dict[str, Path]
    ) -> None:
        """Nilai di .env.migrator tidak boleh berpengaruh pada Guard."""
        migrator = workspace["repo"] / MIGRATOR_ENV_FILENAME
        migrator.write_text(
            "GUARD_DB_URL=postgresql://salah:salah@127.0.0.1:1/salah\nGUARD_HEARTBEAT_SECONDS=59\n",
            encoding="utf-8",
        )
        write_env(workspace)
        loaded = load(workspace)
        assert loaded.heartbeat_seconds == DEFAULT_HEARTBEAT_SECONDS
        assert "salah" not in loaded.db_url

    def test_spy_would_catch_a_regression(
        self, workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Penjaga untuk penjaga: spy benar-benar mencatat."""
        seen: list[Path] = []
        real = config_module.read_env_file
        monkeypatch.setattr(config_module, "read_env_file", lambda p: (seen.append(p), real(p))[1])
        config_module.read_env_file(workspace["repo"] / MIGRATOR_ENV_FILENAME)
        assert [p.name for p in seen] == [MIGRATOR_ENV_FILENAME]


class TestShape:
    def test_config_has_no_other_process_urls(self) -> None:
        """Guard hanya boleh tahu URL-nya sendiri (Arsitektur v1.1 bagian 2)."""
        fields = set(GuardConfig.__dataclass_fields__)
        assert fields == {
            "db_url",
            "mandate_path",
            "signing_key_path",
            "public_keys_path",
            "heartbeat_seconds",
            "lock_timeout_ms",
        }
        assert not [f for f in fields if "migrator" in f]
        assert not [f for f in fields if f.startswith(("approver", "executor", "watcher"))]

    def test_config_is_frozen(self, workspace: dict[str, Path]) -> None:
        write_env(workspace)
        loaded = load(workspace)
        with pytest.raises(AttributeError):
            loaded.heartbeat_seconds = 9  # type: ignore[misc]

    def test_env_var_names_are_the_documented_ones(self) -> None:
        assert set(GUARD_ENV_VARS.values()) == {
            "GUARD_DB_URL",
            "GUARD_MANDATE_PATH",
            "GUARD_SIGNING_KEY_PATH",
            "GUARD_PUBLIC_KEYS_PATH",
            "GUARD_HEARTBEAT_SECONDS",
            "GUARD_LOCK_TIMEOUT_MS",
        }

    def test_error_messages_do_not_leak_the_url(self, workspace: dict[str, Path]) -> None:
        write_env(
            workspace,
            GUARD_DB_URL="postgresql://guard_app:rahasia-sekali@127.0.0.1:5434/d",
            GUARD_MANDATE_PATH=str(workspace["repo"] / "hilang.json"),
        )
        with pytest.raises(GuardConfigError) as exc:
            load(workspace)
        assert "rahasia-sekali" not in str(exc.value)

    def test_repo_root_default_is_the_real_repo(self) -> None:
        """Pemeriksaan path kunci hanya berarti kalau akar repo-nya benar."""
        assert (REPO_ROOT / "pyproject.toml").is_file()
        assert (REPO_ROOT / "src" / "payment_guard").is_dir()
