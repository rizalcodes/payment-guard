"""Akses database: koneksi, redaksi rahasia, dan notice handler.

Aturan modul ini:

* Tidak ada SQL yang dirakit dari string input. Semua kueri yang menerima
  nilai memakai parameter. Satu-satunya SQL dinamis di seluruh ``src/`` ada di
  :mod:`payment_guard.bootstrap`, yang memakai ``psycopg.sql`` composable
  (identifier di-quote oleh driver, bukan oleh f-string).
* URL database adalah rahasia (Arsitektur v1.1 bagian 2.1). URL tidak pernah
  muncul utuh di log, pesan error, atau traceback: lihat :func:`redact_url`.

Nama variabel environment dan pemuatan file env ada di
:mod:`payment_guard.config`, bukan di sini: modul ini hanya soal koneksi.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

import psycopg
from psycopg.conninfo import conninfo_to_dict

from payment_guard.locks import advisory_key

#: Pola notice/warning yang WAJIB menggagalkan migrasi. Postgres memancarkan
#: ``WARNING: no privileges were granted for ...`` bila GRANT tidak berefek,
#: dan ``no privileges could be revoked ...`` untuk REVOKE. Keduanya berarti
#: hak akses tidak jadi seperti yang dimaksud. Security v1.1 E11 / ST-41.
NO_PRIVILEGES_PATTERN: Final = re.compile(r"no privileges", re.IGNORECASE)

#: Severity yang diperiksa terhadap :data:`NO_PRIVILEGES_PATTERN`.
_CHECKED_SEVERITIES: Final = frozenset({"NOTICE", "WARNING"})

_URL_WITH_CREDENTIALS: Final = re.compile(r"(?P<scheme>[a-z+]+://)(?P<userinfo>[^/@\s]*@)?")

# SQL statik. Tidak ada bagian yang berasal dari input; nilai selalu parameter.
#
# ``set_config`` dipakai, bukan ``SET``: ``SET`` tidak menerima parameter, dan
# merakit angka timeout ke dalam SQL akan melanggar aturan "tanpa SQL dirakit
# string" (Security v1.1 E6).
SET_LOCK_TIMEOUT: Final = "SELECT set_config('lock_timeout', %s, false)"
SET_STATEMENT_TIMEOUT: Final = "SELECT set_config('statement_timeout', %s, false)"
SELECT_DB_NOW: Final = "SELECT extract(epoch from now())::bigint"
TAKE_XACT_LOCK: Final = "SELECT pg_advisory_xact_lock(%s)"
TRY_SESSION_LOCK: Final = "SELECT pg_try_advisory_lock(%s)"
RELEASE_SESSION_LOCK: Final = "SELECT pg_advisory_unlock(%s)"


def redact_url(url: str) -> str:
    """Buang kredensial dari URL supaya aman dicetak.

    ``postgresql://user:pw@127.0.0.1:5434/db`` menjadi
    ``postgresql://<redacted>@127.0.0.1:5434/db``. Bentuk yang tidak dikenali
    diganti seluruhnya, bukan dibiarkan lolos.
    """
    if not url:
        return "<empty>"
    match = _URL_WITH_CREDENTIALS.match(url)
    if match is None:
        return "<redacted>"
    rest = url[match.end() :]
    return f"{match.group('scheme')}<redacted>@{rest}"


class MigrationError(RuntimeError):
    """Kegagalan yang terdeteksi runner migrasi atau bootstrap.

    Pesannya ditampilkan ke operator, jadi tidak boleh memuat rahasia.
    """


@dataclass
class NoticeCollector:
    """Menampung notice/warning Postgres selama satu operasi.

    Dipasang ke koneksi lewat ``conn.add_notice_handler``. Runner migrasi
    memeriksa :meth:`offending` setelah menjalankan setiap file dan
    menggagalkan transaksi bila ada yang cocok dengan pola "no privileges".
    """

    messages: list[str] = field(default_factory=list)

    def __call__(self, diagnostic: psycopg.errors.Diagnostic) -> None:
        severity = (diagnostic.severity_nonlocalized or diagnostic.severity or "").upper()
        text = diagnostic.message_primary or ""
        self.messages.append(f"{severity}: {text}")

    def offending(self) -> list[str]:
        """Notice yang berpola "no privileges" pada severity NOTICE/WARNING."""
        hits = []
        for message in self.messages:
            severity, _, text = message.partition(": ")
            if severity in _CHECKED_SEVERITIES and NO_PRIVILEGES_PATTERN.search(text):
                hits.append(message)
        return hits

    def clear(self) -> None:
        self.messages.clear()


#: Batas waktu menyambung, dalam detik, bila URL tidak menentukannya sendiri.
#: Tanpa batas ini, "database mati -> fail closed" (Arsitektur v1.1 bagian 13)
#: berarti menggantung sampai TCP menyerah, bukan gagal dengan cepat.
DEFAULT_CONNECT_TIMEOUT: Final = 10


def open_connection(
    url: str,
    *,
    autocommit: bool = False,
    lock_timeout_ms: int | None = None,
    statement_timeout_ms: int | None = None,
) -> psycopg.Connection:
    """Buka koneksi tanpa context manager, dengan timeout opsional.

    Dipakai untuk koneksi yang hidupnya lebih panjang dari satu blok, yaitu
    koneksi lock dedikasi Guard. Pemanggil bertanggung jawab menutupnya.

    ``lock_timeout`` dan ``statement_timeout`` dipasang **per sesi** lewat
    ``set_config`` yang berparameter; ``SET`` tidak bisa menerima parameter,
    dan merakit SQL dari angka itu akan melanggar aturan "tanpa SQL dirakit
    string". Keduanya di-commit supaya tidak ikut hilang saat transaksi
    pertama di-rollback (Arsitektur v1.1 bagian 4).
    """
    options: dict[str, object] = {"autocommit": autocommit}
    # Jangan menimpa nilai yang sudah ditulis operator di URL.
    if "connect_timeout" not in conninfo_to_dict(url):
        options["connect_timeout"] = DEFAULT_CONNECT_TIMEOUT
    try:
        conn = psycopg.connect(url, **options)  # type: ignore[arg-type]
    except psycopg.OperationalError as exc:
        raise MigrationError(
            f"tidak bisa menyambung ke database ({redact_url(url)}): {_first_line(exc)}"
        ) from None

    if lock_timeout_ms is not None or statement_timeout_ms is not None:
        try:
            with conn.cursor() as cur:
                if lock_timeout_ms is not None:
                    cur.execute(SET_LOCK_TIMEOUT, (str(lock_timeout_ms),))
                if statement_timeout_ms is not None:
                    cur.execute(SET_STATEMENT_TIMEOUT, (str(statement_timeout_ms),))
            conn.commit()
        except psycopg.Error:
            conn.close()
            raise
    return conn


@contextmanager
def connect(
    url: str,
    *,
    autocommit: bool = False,
    lock_timeout_ms: int | None = None,
    statement_timeout_ms: int | None = None,
) -> Iterator[psycopg.Connection]:
    """Buka koneksi ke ``url`` dan tutup saat blok selesai.

    Pesan error koneksi diredaksi: psycopg menyertakan host dan user di
    ``OperationalError``, dan URL bisa saja memuat password.
    """
    conn = open_connection(
        url,
        autocommit=autocommit,
        lock_timeout_ms=lock_timeout_ms,
        statement_timeout_ms=statement_timeout_ms,
    )
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Jam database
# --------------------------------------------------------------------------- #


def db_now(cur: psycopg.Cursor) -> int:
    """Detik UTC dari **jam database**, bukan jam proses.

    Arsitektur v1.1 prinsip 5: semua penilaian waktu memakai jam database.
    Di dalam satu transaksi ``now()`` konstan, jadi memanggil ini dua kali
    dalam satu transaksi memberi nilai yang sama. Itu disengaja: seluruh
    keputusan dinilai pada satu titik waktu.

    Perhatikan ``extract(epoch from now())::bigint`` **membulatkan**, bukan
    memotong. Karena itu setiap timestamp yang ditulis ke ``decisions`` harus
    diturunkan dari bilangan bulat ini, termasuk ``created_at``; memakai
    ``DEFAULT now()`` untuk ``created_at`` akan melanggar CHECK
    ``decided_not_before_created`` setiap kali pembulatan membulatkan ke bawah.
    """
    cur.execute(SELECT_DB_NOW)
    row = cur.fetchone()
    if row is None:  # pragma: no cover - tidak mungkin untuk SELECT skalar
        raise MigrationError("tidak bisa membaca jam database")
    return int(row[0])


# --------------------------------------------------------------------------- #
# Advisory lock
# --------------------------------------------------------------------------- #


def take_xact_lock(cur: psycopg.Cursor, namespace: str, identity: str = "") -> None:
    """Ambil ``pg_advisory_xact_lock`` eksklusif. Menunggu sampai dapat.

    Dilepas otomatis saat transaksi berakhir, jadi tidak ada jalur di mana
    lock tertinggal karena pengecualian.
    """
    cur.execute(TAKE_XACT_LOCK, (advisory_key(namespace, identity),))


def try_session_lock(cur: psycopg.Cursor, namespace: str, identity: str = "") -> bool:
    """Coba ambil lock advisory tingkat **sesi**. Tidak menunggu.

    True berarti dapat; False berarti ada sesi lain yang memegangnya. Lock
    tingkat sesi hidup sampai dilepas atau koneksinya tertutup, yang membuatnya
    tepat untuk "satu Guard per agent" (Arsitektur v1.1 bagian 4).
    """
    cur.execute(TRY_SESSION_LOCK, (advisory_key(namespace, identity),))
    row = cur.fetchone()
    return bool(row[0]) if row is not None else False


def release_session_lock(cur: psycopg.Cursor, namespace: str, identity: str = "") -> bool:
    """Lepas lock sesi yang dipegang koneksi ini."""
    cur.execute(RELEASE_SESSION_LOCK, (advisory_key(namespace, identity),))
    row = cur.fetchone()
    return bool(row[0]) if row is not None else False


# --------------------------------------------------------------------------- #
# Pembacaan nilai
# --------------------------------------------------------------------------- #


def to_int(value: object) -> int:
    """Ubah nilai dari database menjadi ``int``, tanpa pernah lewat float.

    ``NUMERIC(78,0)`` dikembalikan psycopg sebagai :class:`decimal.Decimal`.
    Mengubahnya lewat ``float()`` akan merusak jumlah di atas 2^53, yang untuk
    USDC base units berarti sekitar 9 miliar USDC: besar, tapi bukan mustahil
    dan tidak ada alasan menerima risikonya.

    Raises:
        ValueError: bila nilainya ``None``, bukan bilangan, punya bagian
            pecahan, atau berupa ``float``/``bool``.
    """
    if isinstance(value, bool):
        raise ValueError("bool bukan jumlah yang sah")
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise ValueError(f"nilai {value} punya bagian pecahan; jumlah harus bulat")
        return int(value)
    if value is None:
        raise ValueError("nilai NULL bukan jumlah; pakai coalesce di kueri")
    raise ValueError(f"tidak bisa membaca {type(value).__name__} sebagai jumlah bulat")


def _first_line(exc: BaseException) -> str:
    """Baris pertama pesan pengecualian, tanpa detail koneksi berlebih."""
    return str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
