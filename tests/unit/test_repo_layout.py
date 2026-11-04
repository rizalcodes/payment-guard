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


class TestCanonicalDocs:
    """Dokumen yang mengikat harus ada, dengan nama yang dirujuk kode dan tes.

    Nama berkasnya dirujuk dari docstring di seluruh ``src/``, dari README, dan
    dari ``scripts/demo_execute.md``. Mengganti namanya memutus rujukan itu
    tanpa ada yang gagal, jadi daftar ini yang menahannya.
    """

    CANONICAL = (
        "Payment_Guard_PRD_v2.md",
        "PRD_v2.1.md",
        "Architecture_v1.1.md",
        "Security_v1.1.md",
    )

    #: Dokumen turunan: dihasilkan atau dijaga tes, bukan ditulis bebas.
    DERIVED = ("events.md", "schema.md")

    #: Prosedur manual operator.
    OPERATIONAL = ("runbook.md",)

    #: Catatan riwayat: keputusan dan apa yang terjadi per tag.
    #:
    #: Dipisah dari CANONICAL karena keduanya tidak mengikat perilaku kode —
    #: keduanya menjelaskan mengapa perilaku itu begitu, dan apa yang sudah
    #: diketahui salah di tag yang sudah dibuat.
    HISTORICAL = ("decisions.md", "CHANGELOG.md")

    @pytest.mark.parametrize("name", CANONICAL)
    def test_canonical_document_exists(self, name: str) -> None:
        assert (ROOT / "docs" / name).is_file(), name

    @pytest.mark.parametrize("name", DERIVED)
    def test_derived_document_exists(self, name: str) -> None:
        assert (ROOT / "docs" / name).is_file(), name

    @pytest.mark.parametrize("name", OPERATIONAL)
    def test_operational_document_exists(self, name: str) -> None:
        assert (ROOT / "docs" / name).is_file(), name

    @pytest.mark.parametrize("name", HISTORICAL)
    def test_historical_document_exists(self, name: str) -> None:
        assert (ROOT / "docs" / name).is_file(), name

    def test_no_other_markdown_sneaks_into_docs(self) -> None:
        """Daftar isi ``docs/`` tertutup.

        Bukan untuk kerapian: dokumen yang mengikat dan dokumen catatan
        sementara tidak boleh terlihat sama di direktori yang sama.
        """
        ada = sorted(p.name for p in (ROOT / "docs").glob("*.md"))
        harus = sorted([*self.CANONICAL, *self.DERIVED, *self.OPERATIONAL, *self.HISTORICAL])
        assert ada == harus, f"isi docs/ berubah: {ada}"

    def test_the_runbook_covers_the_stuck_transaction(self) -> None:
        """Keterbatasan yang disebut README harus punya prosedurnya."""
        teks = (ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")
        assert "Transaksi macet" in teks
        assert "nonce" in teks
        assert "jangan menghapus" in teks.lower()

    def test_the_readme_states_the_known_limitations(self) -> None:
        teks = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "## Known limitations" in teks
        assert "bump" in teks
        assert "maxFeePerGas" in teks
        assert "docs/runbook.md" in teks


class TestTheReleaseRule:
    """Aturan rilis M6.0.1 harus tertulis, di runbook dan di README.

    Aturan ini ada karena satu kejadian, bukan karena kerapian: ``m5.1``
    dibuat dengan satu tes merah di dalamnya karena suite dinilai di working
    tree yang masih punya berkas untracked. Aturan yang hanya diingat akan
    hilang bersama sesi yang mengingatnya, jadi ia ditulis — dan tes ini yang
    menahannya tetap tertulis.
    """

    def runbook(self) -> str:
        return (ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")

    def readme(self) -> str:
        return (ROOT / "README.md").read_text(encoding="utf-8")

    def test_the_runbook_has_a_release_section(self) -> None:
        assert "## 7. Rilis" in self.runbook()

    def test_the_runbook_demands_a_clean_checkout(self) -> None:
        teks = self.runbook()
        assert "checkout bersih" in teks
        assert "git stash -u" in teks
        assert "git clone" in teks

    def test_the_runbook_names_the_test_that_caught_this(self) -> None:
        """Aturan tanpa kejadiannya akan dibaca sebagai formalitas."""
        teks = self.runbook()
        assert "test_only_example_env_files_are_tracked" in teks
        assert "untracked" in teks

    def test_the_readme_repeats_the_rule(self) -> None:
        teks = self.readme()
        assert "checkout bersih" in teks
        assert "test_only_example_env_files_are_tracked" in teks
        assert "untracked" in teks


class TestTheDecisionLog:
    """``docs/decisions.md`` memuat keputusan nonce M6.0.1, kata per kata.

    Kalimat yang diminta operator diperiksa apa adanya. Parafrase akan lulus
    tes yang longgar dan kehilangan hal yang justru dikunci: bahwa jaring
    pengamannya tetap ada di database.
    """

    DIMINTA = (
        "Rumus nonce di brief diganti karena nonce 0 tidak pernah terpakai di wallet\n"
        "baru. `UNIQUE(from_addr, nonce)` tetap jaring pengaman terakhir."
    )

    def text(self) -> str:
        return (ROOT / "docs" / "decisions.md").read_text(encoding="utf-8")

    def test_the_locked_sentence_is_present(self) -> None:
        assert self.DIMINTA in self.text()

    def test_it_records_the_attribution_correction(self) -> None:
        teks = self.text()
        assert "Haiku 5.5" in teks
        assert "Opus 5" in teks
        assert "b675851" in teks, "commit yang TIDAK disentuh harus ikut dicatat"

    def test_the_changelog_records_the_red_test_in_m5_1(self) -> None:
        teks = (ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
        assert "test_only_example_env_files_are_tracked" in teks
        assert ".env.test.example" in teks
        assert "Tag `m5.1` tidak dipindahkan." in teks

    def test_the_changelog_records_the_hash_mapping(self) -> None:
        teks = (ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
        for hash_ in ("6c8adf6", "2c72a1d", "b675851"):
            assert hash_ in teks, hash_


class TestTheNonceFormulaIsStatedOnce:
    """Rumus nonce muncul di tiga dokumen, dan harus sama di ketiganya.

    Operator mengunci satu versi rumus. Rumus yang sama ditulis ulang di tiga
    tempat akan menyimpang pada perubahan berikutnya, dan dokumen yang saling
    bertentangan lebih buruk daripada dokumen yang tidak lengkap — pembacanya
    tidak punya cara tahu mana yang berlaku.
    """

    RUMUS = "next_nonce = max(pending_rpc, coalesce(max_db_nonce, -1) + 1)"

    BERKAS = (
        ("docs", "PRD_v2.1.md"),
        ("docs", "Architecture_v1.1.md"),
        ("docs", "runbook.md"),
        ("docs", "decisions.md"),
    )

    @pytest.mark.parametrize(("folder", "name"), BERKAS)
    def test_the_formula_appears_verbatim(self, folder: str, name: str) -> None:
        teks = (ROOT / folder / name).read_text(encoding="utf-8")
        assert self.RUMUS in teks, f"{name} tidak memuat rumus yang dikunci"

    def test_the_code_states_the_same_formula(self) -> None:
        teks = (ROOT / "src" / "payment_guard" / "executor.py").read_text(encoding="utf-8")
        assert self.RUMUS in teks
        assert "coalesce(max(nonce), -1)" in teks

    #: Dokumen yang **menyatakan** apa yang berlaku.
    #:
    #: ``decisions.md`` sengaja tidak termasuk: di sanalah bentuk lama justru
    #: harus dikutip, karena keputusannya tidak bisa dijelaskan tanpa
    #: menunjukkan apa yang ditolak.
    NORMATIF = (
        ("docs", "PRD_v2.1.md"),
        ("docs", "Architecture_v1.1.md"),
        ("docs", "runbook.md"),
    )

    @pytest.mark.parametrize(("folder", "name"), NORMATIF)
    def test_no_stale_variant_survives(self, folder: str, name: str) -> None:
        """Bentuk lama tidak boleh tertinggal di dokumen yang menyatakan.

        Bentuk lama memberi nonce 1 untuk wallet baru. Tertinggal di satu
        dokumen saja sudah cukup untuk membuat orang berikutnya menulis ulang
        kode sesuai dokumen itu.
        """
        teks = (ROOT / folder / name).read_text(encoding="utf-8")
        assert "nonce tertinggi di DB + 1" not in teks
        assert "nonce DB + 1" not in teks
        assert "max(nonce)) + 1" not in teks


class TestLockfileIsHashed:
    """Pemasangan harus bisa diulang, dan isinya bisa diperiksa.

    Lockfile tanpa hash berarti "versi yang sama" tidak menjamin "byte yang
    sama", dan pip-audit di CI mengaudit sesuatu yang belum tentu terpasang.
    """

    def text(self) -> str:
        return (ROOT / "uv.lock").read_text(encoding="utf-8")

    def test_the_lockfile_exists(self) -> None:
        assert (ROOT / "uv.lock").is_file()

    def test_it_carries_sha256_hashes(self) -> None:
        assert 'hash = "sha256:' in self.text()

    def test_there_is_a_hash_for_roughly_every_package(self) -> None:
        """Lebih banyak hash daripada paket: sdist dan beberapa wheel per paket."""
        isi = self.text()
        paket = isi.count("\nname = ")
        hashes = isi.count('hash = "sha256:')
        assert paket > 50, f"lockfile hanya memuat {paket} paket"
        assert hashes >= paket, f"{hashes} hash untuk {paket} paket"

    def test_the_runtime_dependencies_are_all_locked(self) -> None:
        import tomllib

        isi = self.text()
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        for spec in pyproject["project"]["dependencies"]:
            nama = spec.split(">")[0].split("=")[0].split("<")[0].split("[")[0]
            assert f'name = "{nama}"' in isi, nama


class TestContinuousIntegration:
    """Berkas CI ditulis di M6.0 dan belum pernah dijalankan: repo tanpa remote.

    Yang bisa diperiksa sekarang adalah bentuknya, dan satu sifat yang harus
    benar sejak awal: tidak ada rahasia di dalamnya.
    """

    def workflow(self) -> str:
        return (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    def test_the_workflow_exists(self) -> None:
        assert (ROOT / ".github" / "workflows" / "ci.yml").is_file()

    def test_it_is_valid_yaml_with_three_jobs(self) -> None:
        import yaml

        parsed = yaml.safe_load(self.workflow())
        assert set(parsed["jobs"]) == {"lint", "test", "audit"}

    def test_it_contains_no_secrets(self) -> None:
        """Tidak ada ``${{ secrets.* }}``.

        Satu-satunya password di sana adalah password Postgres sementara untuk
        container layanan yang hidup selama satu job, sama dengan yang ada di
        docker-compose.yml.
        """
        assert "secrets." not in self.workflow()

    def effective(self) -> str:
        """Isi workflow **tanpa komentar**.

        Tes di bawah memeriksa apa yang CI benar-benar lakukan, bukan apa yang
        komentarnya sebutkan. Komentar di berkas itu menjelaskan mengapa port
        5434 dan ``PAYMENT_GUARD_LIVE`` justru **tidak** dipakai, dan pencarian
        teks mentah akan menganggap penjelasan itu sebagai pelanggaran.
        """
        return "\n".join(
            baris for baris in self.workflow().splitlines() if not baris.strip().startswith("#")
        )

    def test_the_test_job_never_uses_the_operator_port(self) -> None:
        """5434 adalah cluster operator; CI memakai 5435 seperti harness lokal."""
        import yaml

        parsed = yaml.safe_load(self.workflow())
        ports = parsed["jobs"]["test"]["services"]["postgres"]["ports"]
        assert ports == ["5435:5432"]
        assert "5434" not in self.effective()

    def test_the_test_database_name_satisfies_the_harness(self) -> None:
        """Harness menolak nama database yang tidak berakhiran ``_test``."""
        import yaml

        parsed = yaml.safe_load(self.workflow())
        service = parsed["jobs"]["test"]["services"]["postgres"]
        assert service["env"]["POSTGRES_DB"].endswith("_test")

    def test_live_tests_are_never_enabled(self) -> None:
        """CI tidak boleh membelanjakan gas.

        Dua kunci tes live harus dua-duanya absen dari yang CI jalankan:
        variabel ``PAYMENT_GUARD_LIVE`` dan penanda ``-m live``.
        """
        berjalan = self.effective()
        assert "PAYMENT_GUARD_LIVE" not in berjalan
        assert "-m live" not in berjalan
        assert '-m "live"' not in berjalan

    def test_every_job_installs_from_the_lockfile(self) -> None:
        """``--locked`` menggagalkan CI bila uv.lock basi, bukan memperbaruinya."""
        assert self.workflow().count("uv sync --locked") == 3

    def test_it_runs_ruff_mypy_pytest_and_pip_audit(self) -> None:
        teks = self.workflow()
        for perintah in (
            "ruff check",
            "ruff format --check",
            "mypy --strict",
            "pytest",
            "pip-audit",
        ):
            assert perintah in teks, perintah


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
