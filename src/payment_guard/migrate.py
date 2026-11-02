"""Runner migrasi: SQL murni di atas psycopg 3, forward-only.

Sifat yang dijamin (PRD v2.1 nomor 12, Arsitektur v1.1 bagian 3.2):

* **Forward-only.** Tidak ada ``down``. Perbaikan selalu lewat file baru.
* **Satu transaksi per file.** Seluruh isi file dikirim sebagai satu string
  tanpa parameter; runner tidak pernah memecah file pada titik koma. Gagal di
  tengah file berarti rollback penuh -- tidak ada setengah skema.
* **Checksum.** SHA-256 dari isi file setelah normalisasi CRLF ke LF. File
  yang sudah terapan lalu diedit atau dihapus menggagalkan runner.
* **Tanpa celah dan tanpa nomor ganda.** Versi harus 1..N berurutan.
* **Satu runner.** ``pg_advisory_xact_lock`` namespace ``migrate``.
* **Hak akses yang diam-diam gagal menggagalkan migrasi.** NOTICE atau
  WARNING berpola "no privileges" membatalkan transaksi (Security v1.1 E11).
* **Tanpa BEGIN/COMMIT di file.** Diperiksa statik sebelum apa pun dijalankan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

import psycopg

from payment_guard.db import MigrationError, NoticeCollector, connect
from payment_guard.locks import MIGRATE_LOCK_KEY
from payment_guard.sqltext import checksum, find_transaction_control, normalize_newlines

#: Direktori migrasi relatif terhadap akar repo.
MIGRATIONS_DIRNAME: Final = "migrations"

#: Nama file: empat digit versi, garis bawah, nama snake_case, ``.sql``.
FILENAME_PATTERN: Final = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9][a-z0-9_]*)\.sql$")

# SQL statik. Tidak ada bagian yang berasal dari input; tidak ada perakitan string.
_CREATE_TABLE: Final = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INT  PRIMARY KEY,
  name       TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 128),
  checksum   TEXT NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
_SELECT_APPLIED: Final = (
    "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
)
_SELECT_ONE: Final = "SELECT checksum FROM schema_migrations WHERE version = %s"
_INSERT_APPLIED: Final = (
    "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)"
)
_TAKE_LOCK: Final = "SELECT pg_advisory_xact_lock(%s)"
_TABLE_EXISTS: Final = "SELECT to_regclass('schema_migrations') IS NOT NULL"


@dataclass(frozen=True)
class MigrationFile:
    """Satu file migrasi di disk, beserta checksum isinya."""

    version: int
    name: str
    path: Path
    sql: str
    checksum: str

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}.sql"


@dataclass(frozen=True)
class AppliedMigration:
    """Satu baris ``schema_migrations``."""

    version: int
    name: str
    checksum: str
    applied_at: datetime


def migrations_dir(root: Path | None = None) -> Path:
    """Direktori migrasi. ``root`` default-nya akar repo yang memuat paket ini."""
    base = root if root is not None else Path(__file__).resolve().parents[2]
    return base / MIGRATIONS_DIRNAME


def discover(directory: Path) -> list[MigrationFile]:
    """Baca dan validasi semua file migrasi di ``directory``.

    Memeriksa, dalam urutan ini: nama file, nomor ganda, celah nomor, dan
    kontrol transaksi di dalam file.

    Raises:
        MigrationError: untuk setiap pelanggaran di atas.
    """
    if not directory.is_dir():
        raise MigrationError(f"direktori migrasi tidak ada: {directory}")

    by_version: dict[int, list[Path]] = {}
    unknown: list[str] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix != ".sql":
            continue
        match = FILENAME_PATTERN.match(path.name)
        if match is None:
            unknown.append(path.name)
            continue
        by_version.setdefault(int(match.group("version")), []).append(path)

    if unknown:
        raise MigrationError(
            "nama file migrasi tidak sesuai pola NNNN_nama.sql: " + ", ".join(sorted(unknown))
        )
    if not by_version:
        raise MigrationError(f"tidak ada file migrasi di {directory}")

    duplicates = {v: [p.name for p in paths] for v, paths in by_version.items() if len(paths) > 1}
    if duplicates:
        detail = "; ".join(
            f"{v:04d}: {', '.join(sorted(names))}" for v, names in sorted(duplicates.items())
        )
        raise MigrationError(f"nomor migrasi ganda: {detail}")

    versions = sorted(by_version)
    too_low = [v for v in versions if v < 1]
    if too_low:
        raise MigrationError(
            "nomor migrasi harus mulai dari 0001; ditemukan "
            + ", ".join(f"{v:04d}" for v in too_low)
        )

    expected = list(range(1, len(versions) + 1))
    if versions != expected:
        missing = sorted(set(expected) - set(versions))
        found = ", ".join(f"{v:04d}" for v in versions)
        if missing:
            raise MigrationError(
                "celah pada nomor migrasi: "
                + ", ".join(f"{v:04d}" for v in missing)
                + f" (ditemukan {found})"
            )
        raise MigrationError(
            f"nomor migrasi harus mulai dari 0001 dan berurutan, ditemukan: {found}"
        )

    files: list[MigrationFile] = []
    for version in versions:
        path = by_version[version][0]
        sql = normalize_newlines(path.read_bytes())
        offenders = find_transaction_control(sql)
        if offenders:
            detail = "; ".join(f"baris {line}: {keyword}" for line, keyword in offenders)
            raise MigrationError(
                f"{path.name} memuat kontrol transaksi ({detail}). "
                "Runner yang memegang transaksi; hapus BEGIN/COMMIT dari file."
            )
        match = FILENAME_PATTERN.match(path.name)
        if match is None:  # pragma: no cover - sudah dicocokkan di atas
            raise MigrationError(f"nama file migrasi tidak terbaca: {path.name}")
        files.append(
            MigrationFile(
                version=version,
                name=match.group("name"),
                path=path,
                sql=sql,
                checksum=checksum(sql),
            )
        )
    return files


def _read_applied(cur: psycopg.Cursor) -> list[AppliedMigration]:
    cur.execute(_SELECT_APPLIED)
    return [AppliedMigration(*row) for row in cur.fetchall()]


def _verify_consistency(
    files: list[MigrationFile], applied: list[AppliedMigration]
) -> list[MigrationFile]:
    """Bandingkan keadaan disk dengan keadaan database.

    Mengembalikan daftar file yang belum terapan, dalam urutan versi.

    Raises:
        MigrationError: bila file terapan hilang, checksum berubah, nama
            berubah, atau ada file belum terapan yang nomornya lebih kecil
            dari migrasi yang sudah terapan (forward-only).
    """
    on_disk = {f.version: f for f in files}

    missing = [a for a in applied if a.version not in on_disk]
    if missing:
        raise MigrationError(
            "migrasi yang sudah terapan tidak ada lagi di disk: "
            + ", ".join(f"{a.version:04d}_{a.name}.sql" for a in missing)
            + ". Migrasi forward-only: pulihkan file itu, jangan hapus."
        )

    for row in applied:
        disk = on_disk[row.version]
        if disk.checksum != row.checksum:
            raise MigrationError(
                f"checksum {disk.label} berubah setelah diterapkan.\n"
                f"  di database : {row.checksum}\n"
                f"  di disk     : {disk.checksum}\n"
                "File migrasi yang sudah terapan terkunci. "
                "Buat file migrasi baru untuk mengubah skema."
            )
        if disk.name != row.name:
            raise MigrationError(
                f"migrasi {row.version:04d} berganti nama setelah diterapkan: "
                f"'{row.name}' di database, '{disk.name}' di disk."
            )

    applied_versions = {a.version for a in applied}
    highest_applied = max(applied_versions, default=0)
    pending = [f for f in files if f.version not in applied_versions]
    out_of_order = [f for f in pending if f.version < highest_applied]
    if out_of_order:
        raise MigrationError(
            "file migrasi baru disisipkan di bawah migrasi yang sudah terapan: "
            + ", ".join(f.label for f in out_of_order)
            + f" (tertinggi terapan: {highest_applied:04d}). Migrasi forward-only."
        )
    return pending


def _fail_on_privilege_notices(notices: NoticeCollector, label: str) -> None:
    offenders = notices.offending()
    if offenders:
        raise MigrationError(
            f"{label} memancarkan notice hak akses: "
            + " | ".join(offenders)
            + ". Hak akses tidak jadi seperti yang dimaksud; migrasi dibatalkan "
            "(Security v1.1 E11). Pastikan `guard db bootstrap-roles` sudah jalan."
        )


def _apply_one(conn: psycopg.Connection, notices: NoticeCollector, file: MigrationFile) -> bool:
    """Terapkan satu file dalam satu transaksi. True bila benar-benar diterapkan.

    Lock advisory diambil di awal transaksi, lalu keadaan terapan dibaca ulang
    di dalam lock itu. Dua runner paralel karena itu tidak bisa menerapkan file
    yang sama dua kali.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_TAKE_LOCK, (MIGRATE_LOCK_KEY,))
        cur.execute(_SELECT_ONE, (file.version,))
        row = cur.fetchone()
        if row is not None:
            if row[0] != file.checksum:
                raise MigrationError(
                    f"checksum {file.label} berubah setelah diterapkan "
                    "(terdeteksi saat hendak menerapkan)."
                )
            return False

        notices.clear()
        # Seluruh isi file sebagai SATU string, tanpa parameter. psycopg memakai
        # protokol kueri sederhana di sini, jadi multi-statement dan blok
        # $$ ... $$ jalan utuh tanpa perlu dipecah di sisi Python.
        try:
            cur.execute(file.sql)
        except psycopg.Error as exc:
            raise MigrationError(
                f"{file.label} gagal: {_pg_detail(exc)}. "
                "Transaksi dibatalkan; tidak ada perubahan yang tersisa dari file ini."
            ) from None
        _fail_on_privilege_notices(notices, file.label)

        cur.execute(_INSERT_APPLIED, (file.version, file.name, file.checksum))
        _fail_on_privilege_notices(notices, file.label)
    return True


def _pg_detail(exc: psycopg.Error) -> str:
    """Rangkum error Postgres tanpa membocorkan detail koneksi."""
    diag = getattr(exc, "diag", None)
    parts: list[str] = []
    if diag is not None and diag.message_primary:
        parts.append(diag.message_primary)
        if diag.message_detail:
            parts.append(f"detail: {diag.message_detail}")
        if diag.message_hint:
            parts.append(f"hint: {diag.message_hint}")
    if not parts:
        text = str(exc).strip()
        parts.append(text.splitlines()[0] if text else type(exc).__name__)
    return " -- ".join(parts)


def run_migrate(url: str, directory: Path) -> list[MigrationFile]:
    """Terapkan semua migrasi yang belum terapan. Mengembalikan yang diterapkan."""
    files = discover(directory)
    notices = NoticeCollector()
    done: list[MigrationFile] = []
    with connect(url) as conn:
        conn.add_notice_handler(notices)
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(_TAKE_LOCK, (MIGRATE_LOCK_KEY,))
            cur.execute(_CREATE_TABLE)
            applied = _read_applied(cur)
        pending = _verify_consistency(files, applied)
        for file in pending:
            if _apply_one(conn, notices, file):
                done.append(file)
    return done


@dataclass(frozen=True)
class StatusReport:
    """Hasil ``guard db status``."""

    applied: list[AppliedMigration]
    pending: list[MigrationFile]
    table_exists: bool


def run_status(url: str, directory: Path) -> StatusReport:
    """Laporkan migrasi terapan dan tertunda. Verifikasi yang sama dijalankan."""
    files = discover(directory)
    with connect(url) as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(_TABLE_EXISTS)
        row = cur.fetchone()
        exists = bool(row[0]) if row is not None else False
        applied = _read_applied(cur) if exists else []
    pending = _verify_consistency(files, applied)
    return StatusReport(applied=applied, pending=pending, table_exists=exists)
