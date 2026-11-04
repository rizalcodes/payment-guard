"""Bentuk repo dan konfigurasi M0.

Keputusan di sini bukan selera: port 5434 dipilih karena 5432 dan 5433 sudah
dipakai project lain di mesin pengembangan, ``name: payment-guard`` mencegah
container dan volume tercampur dengan project itu, dan ``migrations/*.sql text
eol=lf`` menjaga checksum migrasi di Windows. Tes ini menahan semuanya supaya
tidak hilang tanpa sengaja.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def compose_text() -> str:
    """Isi docker-compose.yml TANPA baris komentar.

    Komentar di file itu menjelaskan mengapa ``docker-entrypoint-initdb.d``
    dan ``latest`` justru dihindari, jadi memindai mentah akan salah tangkap.
    """
    lines = (ROOT / "docker-compose.yml").read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.strip().startswith("#"))


class TestDirectoryLayout:
    @pytest.mark.parametrize(
        "relative",
        [
            "src/payment_guard",
            "tests/unit",
            "tests/integration",
            "migrations",
            "docs",
            "evals",
        ],
    )
    def test_directory_exists(self, relative: str) -> None:
        assert (ROOT / relative).is_dir(), f"{relative} tidak ada"

    @pytest.mark.parametrize(
        "relative",
        [
            "pyproject.toml",
            "uv.lock",
            ".python-version",
            ".gitattributes",
            ".gitignore",
            ".env.example",
            ".env.migrator.example",
            "docker-compose.yml",
            ".pre-commit-config.yaml",
            "migrations/0001_init.sql",
            "migrations/0002_roles.sql",
            "src/payment_guard/migrate.py",
            "src/payment_guard/locks.py",
            "src/payment_guard/config.py",
            "src/payment_guard/hashing.py",
            "docs/PRD_v2.1.md",
            "docs/Architecture_v1.1.md",
            "docs/Security_v1.1.md",
        ],
    )
    def test_file_exists(self, relative: str) -> None:
        assert (ROOT / relative).is_file(), f"{relative} tidak ada"

    def test_env_is_not_committed_by_accident(self) -> None:
        """``.env`` boleh ada di mesin pengembang, tapi harus ter-gitignore."""
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        assert ".env" in gitignore


class TestPythonVersion:
    def test_pinned_to_312(self) -> None:
        assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.12"

    def test_pyproject_agrees(self, pyproject: dict) -> None:
        assert pyproject["project"]["requires-python"] == "==3.12.*"


class TestTestLayout:
    """Tata letak tes itu sendiri, sejauh ia bisa membuat suite tidak jalan."""

    def test_no_two_test_modules_share_a_basename(self) -> None:
        """``uv run pytest`` mengumpulkan seluruh ``tests/`` sekaligus.

        ``tests/unit`` dan ``tests/integration`` tidak punya ``__init__.py``,
        jadi pytest mengimpor tiap berkas tes sebagai modul tingkat atas dengan
        nama basename-nya. Dua berkas bernama sama akan menggagalkan
        **pengumpulan**, bukan satu tes — seluruh suite berhenti sebelum
        berjalan.

        Penjaga ini ada karena hal itu benar-benar terjadi: M5 menambahkan
        ``tests/integration/test_executor.py`` di samping
        ``tests/unit/test_executor.py``, dan tidak tertangkap karena keduanya
        hanya pernah dijalankan terpisah.
        """
        by_name: dict[str, list[str]] = {}
        for path in sorted((ROOT / "tests").rglob("test_*.py")):
            by_name.setdefault(path.name, []).append(str(path.relative_to(ROOT)).replace("\\", "/"))
        bentrok = {name: paths for name, paths in by_name.items() if len(paths) > 1}
        assert bentrok == {}, f"basename ganda: {bentrok}"

    def test_the_guard_would_catch_a_collision(self) -> None:
        """Penjaga untuk penjaga: ia memang membandingkan basename."""
        contoh = {"test_x.py": ["tests/unit/test_x.py", "tests/integration/test_x.py"]}
        assert {k: v for k, v in contoh.items() if len(v) > 1} == contoh

    def test_test_directories_have_no_init_files(self) -> None:
        """Kalau kelak ada ``__init__.py``, aturan di atas tidak lagi berlaku.

        Tes ini bukan larangan; ia penanda. Menambah ``__init__.py`` adalah
        pilihan yang sah, tapi ia mengubah cara pytest mengimpor dan membuat
        impor datar seperti ``from conftest import ...`` berhenti bekerja.
        """
        assert list((ROOT / "tests").rglob("__init__.py")) == []


class TestDependencies:
    def test_minimal_runtime_dependencies(self, pyproject: dict) -> None:
        names = [
            d.split(">")[0].split("=")[0].split("<")[0]
            for d in pyproject["project"]["dependencies"]
        ]
        assert "psycopg[binary]" in names
        assert "cryptography" in names, "disiapkan untuk M2 (signing)"
        assert "pycryptodome" in names, "Keccak-256 untuk M1 dan M5"
        assert "mcp" in names, "SDK MCP resmi untuk server Guard (M3)"
        assert "web3" in names, "RPC dan tanda tangan transaksi untuk Executor (M5)"
        assert len(names) == 5, f"dependensi runtime harus minimal, ada: {names}"

    def test_web3_is_pinned_and_confined(self, pyproject: dict) -> None:
        """PRD v2 bagian 5 mengizinkan web3.py; batas versinya tetap wajib.

        ``web3`` membawa puluhan paket transitif, jadi ia satu-satunya
        dependensi M5 dan hanya ``chain.py`` yang boleh mengimpornya; penjaga
        impornya ada di ``test_source_hygiene.py``.
        """
        spec = next(d for d in pyproject["project"]["dependencies"] if d.startswith("web3"))
        assert ">=" in spec and "<" in spec, f"web3 harus dibatasi versinya: {spec}"

    def test_lockfile_pins_web3(self) -> None:
        assert 'name = "web3"' in (ROOT / "uv.lock").read_text(encoding="utf-8")

    def test_mcp_is_the_official_sdk_and_pinned(self, pyproject: dict) -> None:
        """PRD v2 bagian 5: SDK resmi Python, bukan implementasi protokol sendiri."""
        spec = next(d for d in pyproject["project"]["dependencies"] if d.startswith("mcp"))
        assert ">=" in spec and "<" in spec, f"mcp harus dibatasi versinya: {spec}"

    def test_lockfile_pins_mcp(self) -> None:
        assert 'name = "mcp"' in (ROOT / "uv.lock").read_text(encoding="utf-8")

    def test_psycopg_is_version_3(self, pyproject: dict) -> None:
        spec = next(d for d in pyproject["project"]["dependencies"] if d.startswith("psycopg"))
        assert ">=3" in spec and "<4" in spec

    def test_dev_tools_present(self, pyproject: dict) -> None:
        dev = " ".join(pyproject["dependency-groups"]["dev"])
        assert "pytest" in dev
        assert "ruff" in dev

    def test_no_orm_or_migration_framework(self, pyproject: dict) -> None:
        """PRD v2.1 nomor 12: SQL murni, tanpa Alembic atau SQLAlchemy."""
        everything = " ".join(
            [*pyproject["project"]["dependencies"], *pyproject["dependency-groups"]["dev"]]
        ).lower()
        for forbidden in ("alembic", "sqlalchemy", "django", "asyncpg"):
            assert forbidden not in everything

    def test_keccak_backend_is_pycryptodome_not_pysha3(self, pyproject: dict) -> None:
        """pysha3 tidak dirawat lagi dan tidak mendukung Python 3.12."""
        everything = " ".join(
            [*pyproject["project"]["dependencies"], *pyproject["dependency-groups"]["dev"]]
        ).lower()
        assert "pysha3" not in everything
        assert "sha3" not in everything
        assert "pycryptodome" in everything

    def test_lockfile_pins_pycryptodome(self) -> None:
        assert 'name = "pycryptodome"' in (ROOT / "uv.lock").read_text(encoding="utf-8")

    def test_guard_entry_point(self, pyproject: dict) -> None:
        assert pyproject["project"]["scripts"]["guard"] == "payment_guard.cli:main"


class TestLockfile:
    def test_exists_and_has_hashes(self) -> None:
        """Security v1.1 E7: lockfile ber-hash."""
        text = (ROOT / "uv.lock").read_text(encoding="utf-8")
        assert text.count('hash = "sha256:') > 10

    def test_pins_psycopg_and_pytest(self) -> None:
        text = (ROOT / "uv.lock").read_text(encoding="utf-8")
        assert 'name = "psycopg"' in text
        assert 'name = "pytest"' in text


class TestGitAttributes:
    def test_migrations_are_forced_to_lf(self) -> None:
        """Checksum migrasi dihitung atas isi; CRLF di Windows tidak boleh ikut."""
        lines = {
            line.strip()
            for line in (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
        }
        assert "migrations/*.sql text eol=lf" in lines

    def test_migration_files_on_disk_use_lf(self) -> None:
        for path in sorted((ROOT / "migrations").glob("*.sql")):
            assert b"\r\n" not in path.read_bytes(), f"{path.name} memakai CRLF"


class TestDockerCompose:
    def test_project_name_is_set(self, compose_text: str) -> None:
        """Supaya container dan volume tidak tercampur dengan project lain."""
        assert "name: payment-guard" in compose_text

    def test_postgres_16_is_pinned(self, compose_text: str) -> None:
        assert "image: postgres:16." in compose_text
        assert "latest" not in compose_text

    def test_host_port_is_5434_bound_to_loopback(self, compose_text: str) -> None:
        """5432 dan 5433 sudah dipakai project lain di mesin pengembangan."""
        assert '"127.0.0.1:5434:5432"' in compose_text
        assert "5432:5432" not in compose_text
        assert "5433:5432" not in compose_text

    def test_volume_is_named_explicitly(self, compose_text: str) -> None:
        assert "payment-guard-pgdata" in compose_text

    def test_schema_is_not_loaded_by_initdb(self, compose_text: str) -> None:
        """Skema hanya lewat `guard db migrate`, supaya schema_migrations jujur."""
        assert "docker-entrypoint-initdb.d" not in compose_text

    def test_statements_are_not_logged(self, compose_text: str) -> None:
        """CREATE ROLE memuat password; jangan sampai masuk log server."""
        assert "log_statement=none" in compose_text


class TestPreCommit:
    def test_gitleaks_hook_is_configured(self) -> None:
        """Security v1.1 I1 / ST-19."""
        text = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        assert "gitleaks" in text

    def test_ruff_hook_is_configured(self) -> None:
        text = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        assert "ruff" in text

    def test_private_key_detection_is_configured(self) -> None:
        text = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        assert "detect-private-key" in text

    def test_hook_versions_are_pinned(self) -> None:
        """RR-8: versi dipin, bukan mengambang."""
        text = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        revs = [line.strip() for line in text.splitlines() if line.strip().startswith("rev:")]
        assert revs, "tidak ada rev yang dipin"
        for rev in revs:
            value = rev.split(":", 1)[1].strip()
            assert value not in {"main", "master", "HEAD", ""}, rev
