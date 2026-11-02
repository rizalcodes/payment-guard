"""Kunci advisory Postgres.

Spesifikasi (Arsitektur v1.1 bagian 4), dikunci dan diuji known-answer:

    kunci = int64 bertanda dari 8 byte pertama (big-endian) dari
            sha256(f"{namespace}:{identitas}")

Namespace adalah konstanta tertutup. Lock tanpa identitas (``migrate``)
memakai identitas string kosong, sehingga formulanya tetap satu jalur kode
tanpa kasus khusus.

Modul ini murni: tidak menyentuh database, jaringan, atau jam.
"""

from __future__ import annotations

import hashlib
from typing import Final

#: Batas int64 bertanda. Nilai kunci selalu berada di dalam rentang ini.
INT64_MIN: Final = -(2**63)
INT64_MAX: Final = 2**63 - 1

#: Namespace yang sah. Di luar daftar ini ditolak, supaya tidak ada
#: tabrakan ruang nama kunci yang tidak disengaja.
NAMESPACES: Final[frozenset[str]] = frozenset(
    {
        "guard",  # guard:<agent_id>      sesi, satu Guard per agent
        "agent",  # agent:<agent_id>      transaksi, serialisasi jatah
        "mandate",  # mandate:<agent_id>  transaksi, pergantian mandate vs execution
        "executor",  # executor:<from_addr> transaksi, satu penulis nonce
        "watcher",  # watcher:<chain_id>  sesi, satu Watcher per chain
        "migrate",  # migrate:            transaksi, satu runner migrasi
    }
)


def advisory_key(namespace: str, identity: str = "") -> int:
    """Hitung kunci advisory bigint untuk ``namespace`` dan ``identity``.

    Mengembalikan int64 bertanda, siap dikirim ke varian bigint
    ``pg_advisory_xact_lock`` / ``pg_advisory_lock`` dan turunannya.

    Raises:
        ValueError: bila ``namespace`` bukan salah satu dari :data:`NAMESPACES`.
    """
    if namespace not in NAMESPACES:
        raise ValueError(f"namespace kunci tidak dikenal: {namespace!r}")
    digest = hashlib.sha256(f"{namespace}:{identity}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


#: Kunci runner migrasi. Tidak punya identitas.
MIGRATE_LOCK_KEY: Final[int] = advisory_key("migrate")
