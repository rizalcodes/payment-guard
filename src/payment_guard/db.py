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
from typing import Final

import psycopg
from psycopg.conninfo import conninfo_to_dict

#: Pola notice/warning yang WAJIB menggagalkan migrasi. Postgres memancarkan
#: ``WARNING: no privileges were granted for ...`` bila GRANT tidak berefek,
#: dan ``no privileges could be revoked ...`` untuk REVOKE. Keduanya berarti
#: hak akses tidak jadi seperti yang dimaksud. Security v1.1 E11 / ST-41.
NO_PRIVILEGES_PATTERN: Final = re.compile(r"no privileges", re.IGNORECASE)

#: Severity yang diperiksa terhadap :data:`NO_PRIVILEGES_PATTERN`.
_CHECKED_SEVERITIES: Final = frozenset({"NOTICE", "WARNING"})

_URL_WITH_CREDENTIALS: Final = re.compile(r"(?P<scheme>[a-z+]+://)(?P<userinfo>[^/@\s]*@)?")


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


@contextmanager
def connect(url: str, *, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """Buka koneksi ke ``url``.

    Pesan error koneksi diredaksi: psycopg menyertakan host dan user di
    ``OperationalError``, dan URL bisa saja memuat password.
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
    try:
        yield conn
    finally:
        conn.close()


def _first_line(exc: BaseException) -> str:
    """Baris pertama pesan pengecualian, tanpa detail koneksi berlebih."""
    return str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
