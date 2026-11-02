"""Rahasia tidak boleh bocor: redaksi URL, penulisan .env, dan .env.example.

Security v1.1 I1, I2, I6.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from payment_guard.bootstrap import (
    ENV_KEY_FOR_PROCESS,
    MIGRATOR_ENV_KEY,
    BootstrapResult,
    build_url,
)
from payment_guard.config import ADMIN_URL_ENV_VAR, MIGRATOR_URL_ENV_VAR, RUNTIME_URL_ENV_VARS
from payment_guard.config import read_env_file as read_env
from payment_guard.config import write_env_file as write_env
from payment_guard.db import redact_url

ROOT = Path(__file__).resolve().parents[2]
# Canary palsu untuk tes kebocoran. Bukan kredensial nyata.
SECRET = "sup3rs3cr3t-password"


class TestRedactUrl:
    def test_password_is_removed(self) -> None:
        out = redact_url(f"postgresql://guard_app:{SECRET}@127.0.0.1:5434/payment_guard")
        assert SECRET not in out
        assert "guard_app" not in out
        assert "127.0.0.1:5434/payment_guard" in out

    def test_host_and_database_are_kept_for_diagnosis(self) -> None:
        out = redact_url("postgresql://u:p@db.internal:5432/mydb")
        assert out == "postgresql://<redacted>@db.internal:5432/mydb"

    def test_url_without_credentials(self) -> None:
        assert redact_url("postgresql://127.0.0.1:5434/db") == (
            "postgresql://<redacted>@127.0.0.1:5434/db"
        )

    def test_unrecognised_shape_is_redacted_entirely(self) -> None:
        """Bentuk kata kunci libpq juga memuat password; jangan dibiarkan lolos."""
        out = redact_url(f"host=127.0.0.1 user=guard_app password={SECRET}")
        assert out == "<redacted>"
        assert SECRET not in out

    def test_empty(self) -> None:
        assert redact_url("") == "<empty>"


class TestBuildUrl:
    def test_inherits_host_port_and_database_from_admin_url(self) -> None:
        url = build_url("postgresql://admin:pw@127.0.0.1:5434/payment_guard", "guard_app", SECRET)
        assert url == f"postgresql://guard_app:{SECRET}@127.0.0.1:5434/payment_guard"

    def test_accepts_libpq_keyword_form(self) -> None:
        url = build_url("host=127.0.0.1 port=5434 dbname=payment_guard user=admin", "x", "p")
        assert url == "postgresql://x:p@127.0.0.1:5434/payment_guard"

    def test_special_characters_are_percent_encoded(self) -> None:
        url = build_url("postgresql://a@h:1/d", "r", "a/b@c:d?e#f")
        assert "a%2Fb%40c%3Ad%3Fe%23f" in url
        assert url.count("@") == 1


class TestEnvFile:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        write_env(path, {"GUARD_DB_URL": "postgresql://a:b@h:1/d"})
        assert read_env(path)["GUARD_DB_URL"] == "postgresql://a:b@h:1/d"

    def test_existing_keys_are_replaced_in_place(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("GUARD_DB_URL=lama\nRPC_URL=jangan-sentuh\n", encoding="utf-8")
        write_env(path, {"GUARD_DB_URL": "baru"})
        values = read_env(path)
        assert values == {"GUARD_DB_URL": "baru", "RPC_URL": "jangan-sentuh"}

    def test_comments_and_blank_lines_survive(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("# catatan penting\n\nRPC_URL=x\n", encoding="utf-8")
        write_env(path, {"GUARD_DB_URL": "y"})
        text = path.read_text(encoding="utf-8")
        assert "# catatan penting" in text
        assert read_env(path)["RPC_URL"] == "x"

    def test_new_keys_are_appended(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("RPC_URL=x\n", encoding="utf-8")
        written = write_env(path, {"GUARD_DB_URL": "y", "EXECUTOR_DB_URL": "z"})
        assert sorted(written) == ["EXECUTOR_DB_URL", "GUARD_DB_URL"]
        assert read_env(path)["GUARD_DB_URL"] == "y"

    def test_no_updates_leaves_file_untouched(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("RPC_URL=x\n", encoding="utf-8")
        before = path.read_bytes()
        assert write_env(path, {}) == []
        assert path.read_bytes() == before

    def test_written_file_uses_lf(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        write_env(path, {"GUARD_DB_URL": "a"})
        assert b"\r\n" not in path.read_bytes()

    def test_read_env_on_missing_file(self, tmp_path: Path) -> None:
        assert read_env(tmp_path / "tidak_ada") == {}


class TestBootstrapResultNeverLeaks:
    def test_summary_has_no_urls_or_passwords(self, tmp_path: Path) -> None:
        result = BootstrapResult(
            login_roles_created=["guard_app"],
            env_keys_written=["GUARD_DB_URL"],
            migrator_env_keys_written=["MIGRATOR_DB_URL"],
            env_path=tmp_path / ".env",
            migrator_env_path=tmp_path / ".env.migrator",
        )
        text = "\n".join(result.summary_lines())
        # Hanya NAMA kunci, bukan nilainya.
        assert "GUARD_DB_URL" in text
        assert "MIGRATOR_DB_URL" in text
        assert "postgresql://" not in text
        assert SECRET not in text

    def test_summary_reports_both_files(self, tmp_path: Path) -> None:
        result = BootstrapResult(
            env_path=tmp_path / ".env", migrator_env_path=tmp_path / ".env.migrator"
        )
        text = "\n".join(result.summary_lines())
        assert str(tmp_path / ".env") in text
        assert str(tmp_path / ".env.migrator") in text

    def test_result_dataclass_has_no_password_field(self) -> None:
        fields = set(BootstrapResult.__dataclass_fields__)
        assert not {f for f in fields if "password" in f and f != "login_roles_password_reset"}


class TestEnvExample:
    """``.env.example`` boleh di-commit, jadi ia tidak boleh punya nilai."""

    def test_exists(self) -> None:
        assert (ROOT / ".env.example").is_file()

    def test_has_no_values(self) -> None:
        allowed_defaults = {
            "CHAIN_ID",
            "CONFIRMATIONS",
            "SCAN_BATCH_SIZE",
            "REORG_LOOKBACK",
            "GUARD_PUBLIC_KEYS_PATH",
        }
        for key, value in read_env(ROOT / ".env.example").items():
            if key in allowed_defaults:
                continue
            assert value == "", f"{key} punya nilai di .env.example"

    def test_lists_every_runtime_url(self) -> None:
        keys = set(read_env(ROOT / ".env.example"))
        assert set(RUNTIME_URL_ENV_VARS.values()) <= keys
        assert set(ENV_KEY_FOR_PROCESS.values()) <= keys

    def test_does_not_list_the_migrator_url(self) -> None:
        """URL pemilik objek hidup di .env.migrator, bukan di sini."""
        assert MIGRATOR_ENV_KEY not in read_env(ROOT / ".env.example")
        assert MIGRATOR_URL_ENV_VAR not in read_env(ROOT / ".env.example")

    def test_admin_url_is_absent_by_design(self) -> None:
        """Security v1.1 I6: URL admin tidak pernah ada di .env aplikasi."""
        assert ADMIN_URL_ENV_VAR not in read_env(ROOT / ".env.example")


class TestGitignore:
    @pytest.mark.parametrize(
        "pattern",
        [".env", ".env.*", "*.key", "*.pem", "evals/results/", ".venv/", "secrets/"],
    )
    def test_covers_secret_paths(self, pattern: str) -> None:
        lines = {
            line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        }
        assert pattern in lines

    @pytest.mark.parametrize("pattern", ["!.env.example", "!.env.migrator.example"])
    def test_example_files_are_re_included(self, pattern: str) -> None:
        assert pattern in (ROOT / ".gitignore").read_text(encoding="utf-8")


class TestNoHardcodedSecrets:
    """Tidak ada URL database lengkap yang ditulis literal di ``src/``.

    Skema ``postgresql://`` sendirian boleh: :func:`build_url` menyusun URL
    dari potongan. Yang dilarang adalah URL yang sudah berisi host atau
    kredensial.
    """

    #: Skema diikuti setidaknya satu karakter: itu berarti ada isi, bukan
    #: sekadar awalan yang nanti disambung.
    FULL_URL = re.compile(r"postgres(?:ql)?://\S")

    @staticmethod
    def _docstring_nodes(tree: ast.AST) -> set[int]:
        """Identitas node Constant yang merupakan docstring.

        Docstring dikecualikan: contoh URL di docstring ``redact_url`` adalah
        dokumentasi yang memang perlu ada, bukan kredensial.
        """
        ids: set[int] = set()
        holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        for node in ast.walk(tree):
            if isinstance(node, holders) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    if isinstance(first.value.value, str):
                        ids.add(id(first.value))
        return ids

    def test_no_connection_strings_in_source(self) -> None:
        offenders = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = self._docstring_nodes(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if id(node) in docstrings:
                        continue
                    if self.FULL_URL.search(node.value):
                        offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], offenders

    def test_checker_catches_a_planted_url(self) -> None:
        assert self.FULL_URL.search("postgresql://u:p@h:5434/d")
        assert not self.FULL_URL.search("postgresql://")
