"""``guard serve``: sambungan CLI ke server MCP.

Server MCP sendiri diuji di ``tests/unit/test_mcp_schema.py`` (skema) dan
``tests/integration/test_mcp_server.py`` (pemanggilan tool). Yang diuji di sini
hanya sambungannya: subcommand-nya ada, default path-nya benar, dan ia tidak
pernah menyentuh ``.env.migrator``.

Satu tes di bawah menjaga sifat yang mudah hilang tanpa sengaja: ``guard
verify`` dirancang bekerja tanpa database dan tanpa jaringan, jadi
``mcp_server`` (yang menarik FastMCP dan seluruh jalur keputusan) harus
diimpor di dalam handler ``serve``, bukan di tingkat modul ``cli``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from payment_guard.cli import build_parser
from payment_guard.config import RUNTIME_ENV_FILENAME

CLI_MODULE = Path(__file__).resolve().parents[2] / "src" / "payment_guard" / "cli.py"
REPO_ROOT = CLI_MODULE.parents[2]


class TestServeSubcommand:
    def test_subcommand_exists(self) -> None:
        args = build_parser().parse_args(["serve"])
        assert args.handler.__name__ == "_cmd_serve"

    def test_env_file_defaults_to_none_before_main_fills_it(self) -> None:
        """``main`` yang mengisi default, supaya akar repo dihitung sekali."""
        assert build_parser().parse_args(["serve"]).env_file is None

    def test_env_file_can_be_given(self, tmp_path: Path) -> None:
        args = build_parser().parse_args(["serve", "--env-file", str(tmp_path / ".env")])
        assert args.env_file == tmp_path / ".env"

    def test_serve_has_no_migrator_env_file_argument(self) -> None:
        """Guard runtime tidak pernah punya jalur ke URL migrator."""
        args = build_parser().parse_args(["serve"])
        assert not hasattr(args, "migrator_env_file")

    def test_serve_has_no_signing_key_argument(self) -> None:
        """Path kunci datang dari konfigurasi, bukan dari baris perintah.

        Kalau ia bisa diberikan lewat argumen, pemeriksaan "kunci harus di luar
        akar repo" di :func:`payment_guard.config.load_guard_config` bisa
        dilewati dengan satu flag.
        """
        args = build_parser().parse_args(["serve"])
        assert not hasattr(args, "signing_key_file")

    def test_serve_takes_nothing_but_the_runtime_env_file(self) -> None:
        """Permukaan sekecil mungkin: satu argumen, dan itu pun opsional.

        Diperiksa lewat namespace hasil parse, bukan lewat teks bantuan: teks
        bantuan memuat kalimat "tidak pernah membaca .env.migrator", jadi
        memindai teksnya akan salah tangkap.
        """
        args = vars(build_parser().parse_args(["serve"]))
        assert set(args) == {"group", "handler", "env_file"}


class TestMainWiring:
    """``main`` mengisi default dan meneruskan kode keluar apa adanya.

    ``serve`` yang sesungguhnya digantikan: menjalankannya akan membuka
    database dan transport stdio, yang bukan urusan tes unit ini.
    """

    @pytest.fixture
    def dilihat(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, Path]]:
        from payment_guard import mcp_server

        tercatat: list[tuple[Path, Path]] = []

        def palsu(env_file: Path, *, repo_root: Path) -> int:
            tercatat.append((env_file, repo_root))
            return 7

        monkeypatch.setattr(mcp_server, "serve", palsu)
        return tercatat

    def test_default_env_file_is_the_runtime_one_at_the_repo_root(
        self, dilihat: list[tuple[Path, Path]]
    ) -> None:
        from payment_guard.cli import main

        assert main(["serve"]) == 7
        assert dilihat == [(REPO_ROOT / RUNTIME_ENV_FILENAME, REPO_ROOT)]

    def test_an_explicit_env_file_is_passed_through(
        self, tmp_path: Path, dilihat: list[tuple[Path, Path]]
    ) -> None:
        from payment_guard.cli import main

        berkas = tmp_path / "lain.env"
        assert main(["serve", "--env-file", str(berkas)]) == 7
        assert dilihat[0][0] == berkas


class TestOtherCommandsUntouched:
    @pytest.mark.parametrize(
        ("argv", "handler"),
        [
            (["db", "migrate"], "_cmd_migrate"),
            (["db", "status"], "_cmd_status"),
            (["db", "bootstrap-roles"], "_cmd_bootstrap_roles"),
            (["keygen"], "_cmd_keygen"),
            (["verify", "envelope.json"], "_cmd_verify"),
        ],
    )
    def test_handlers_unchanged(self, argv: list[str], handler: str) -> None:
        assert build_parser().parse_args(argv).handler.__name__ == handler


class TestLazyImport:
    """``cli`` tidak boleh menarik server MCP hanya untuk menjalankan ``verify``."""

    def test_mcp_server_is_imported_inside_the_handler(self) -> None:
        tree = ast.parse(CLI_MODULE.read_text(encoding="utf-8"))
        tingkat_modul = [
            node
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("mcp_server")
        ]
        assert tingkat_modul == [], "mcp_server diimpor di tingkat modul cli.py"

    def test_the_handler_does_import_it(self) -> None:
        tree = ast.parse(CLI_MODULE.read_text(encoding="utf-8"))
        handler = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_cmd_serve"
        )
        disebut = [
            node.module
            for node in ast.walk(handler)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        ]
        assert "payment_guard.mcp_server" in disebut

    def test_importing_cli_does_not_import_fastmcp(self) -> None:
        """Dijalankan di subproses supaya tes lain tidak mencemari sys.modules."""
        import subprocess
        import sys

        kode = (
            "import sys; import payment_guard.cli; "
            "print(any(m == 'mcp' or m.startswith('mcp.') for m in sys.modules))"
        )
        # S603 dimatikan dengan sengaja, seperti di tests/integration/test_gitignore.py:
        # argumennya konstanta di berkas ini ditambah sys.executable, bukan masukan.
        hasil = subprocess.run(  # noqa: S603
            [sys.executable, "-c", kode], capture_output=True, text=True, check=True
        )
        assert hasil.stdout.strip() == "False", "mengimpor cli ikut memuat mcp"
