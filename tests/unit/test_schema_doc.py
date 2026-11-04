"""``docs/schema.md`` tidak boleh menyimpang dari ``migrations/``.

Dokumen turunan yang tidak diperiksa akan basi, dan dokumen skema yang basi
lebih buruk daripada tidak ada: ia dipakai orang untuk memutuskan apa yang
boleh ditulis ke mana.

Generatornya hidup di ``scripts/generate_schema_doc.py`` — sebuah alat, bukan
kode pustaka, jadi ia tidak ikut dikirim di dalam paket. Tes ini memuatnya dari
path supaya tidak ada yang perlu menambahkan ``scripts/`` ke ``sys.path``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts" / "generate_schema_doc.py"
DOC = ROOT / "docs" / "schema.md"
MIGRATIONS = ROOT / "migrations"


def load_generator() -> ModuleType:
    """Muat generator dari path, tanpa menyentuh ``sys.path``.

    Modulnya didaftarkan ke ``sys.modules`` **sebelum** dieksekusi, dan itu
    bukan kerapian: ``@dataclass`` mencari namespace modulnya lewat
    ``sys.modules[cls.__module__]`` untuk menyelesaikan anotasi, jadi tanpa
    pendaftaran itu setiap dataclass di dalamnya gagal dibangun.
    """
    spec = importlib.util.spec_from_file_location("generate_schema_doc", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    return load_generator()


def test_the_generator_exists() -> None:
    """Penjaga: tanpa generatornya, tes di bawah tidak memeriksa apa pun."""
    assert GENERATOR.is_file()


def test_the_document_exists() -> None:
    assert DOC.is_file()


def test_the_document_matches_the_migrations(generator: ModuleType) -> None:
    """Inti tes ini: regenerate dan bandingkan byte-per-byte.

    Gagal di sini berarti migrasi berubah dan dokumennya belum. Perbaikannya
    satu perintah, dan pesannya menyebutkannya.
    """
    expected = generator.render(MIGRATIONS)
    current = DOC.read_text(encoding="utf-8")
    assert current == expected, (
        "docs/schema.md tidak sesuai migrations/. Jalankan:\n"
        "    uv run --no-sync python scripts/generate_schema_doc.py"
    )


def test_the_check_mode_agrees(generator: ModuleType) -> None:
    """``--check`` dipakai CI; ia harus sepakat dengan tes ini."""
    assert generator.main(["--check"]) == 0


def test_every_migration_table_is_documented(generator: ModuleType) -> None:
    """Arah sebaliknya: tidak ada tabel yang hilang dari dokumen."""
    tables = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        tables.extend(generator.parse_tables(path.read_text(encoding="utf-8"), path.name))
    teks = DOC.read_text(encoding="utf-8")
    assert tables, "parser tidak menemukan tabel sama sekali"
    for table in tables:
        assert f"### `{table.name}`" in teks, table.name


def test_the_table_count_guard_is_accurate(generator: ModuleType) -> None:
    """``EXPECTED_TABLE_COUNT`` harus cocok dengan migrasi yang ada.

    Penjaga itu yang mencegah parser yang berhenti mengerti menghasilkan
    dokumen kosong; kalau angkanya sendiri salah, ia tidak menjaga apa pun.
    """
    tables = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        tables.extend(generator.parse_tables(path.read_text(encoding="utf-8"), path.name))
    assert len(tables) == generator.EXPECTED_TABLE_COUNT


def test_schema_migrations_is_not_defined_in_sql(generator: ModuleType) -> None:
    """Ia dibuat runner migrasi, bukan oleh migrasi.

    Tabel yang mencatat migrasi mana yang sudah jalan harus ada sebelum migrasi
    pertama dijalankan, jadi ia tidak bisa lahir dari sebuah migrasi.
    """
    for path in sorted(MIGRATIONS.glob("*.sql")):
        for table in generator.parse_tables(path.read_text(encoding="utf-8"), path.name):
            assert table.name != "schema_migrations"


def test_the_document_says_it_is_generated() -> None:
    """Supaya tidak ada yang mengeditnya tangan lalu kehilangan suntingannya."""
    teks = DOC.read_text(encoding="utf-8")
    assert "Jangan diedit tangan" in teks
    assert "scripts/generate_schema_doc.py" in teks


def test_the_document_is_ascii_only() -> None:
    """Berkas turunan yang dibandingkan byte-per-byte sebaiknya ASCII.

    Satu hal lebih sedikit yang bisa berbeda antara mesin pengembang dan CI.
    """
    teks = DOC.read_text(encoding="utf-8")
    bukan_ascii = sorted({c for c in teks if ord(c) > 127})
    assert bukan_ascii == [], f"karakter non-ASCII di docs/schema.md: {bukan_ascii}"


def test_grants_are_summarised(generator: ModuleType) -> None:
    """Bagian hak akses harus menyebut role grup dan tabelnya."""
    teks = DOC.read_text(encoding="utf-8")
    assert "## Hak akses, ringkas" in teks
    for role in ("guard_rw", "approver_rw", "executor_rw", "watcher_rw", "reconciler_rw"):
        assert f"`{role}`" in teks, role
    for table in ("decisions", "executions", "audit_log"):
        assert f"`{table}`" in teks, table


def test_named_checks_are_listed(generator: ModuleType) -> None:
    """CHECK bernama adalah aturan yang paling sering dicari orang."""
    teks = DOC.read_text(encoding="utf-8")
    for constraint in (
        "status_reason_pairs",
        "approved_is_complete",
        "decision_state_timestamps",
        "broadcast_has_timestamp",
    ):
        assert constraint in teks, constraint
