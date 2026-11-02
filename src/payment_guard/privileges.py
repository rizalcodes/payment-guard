"""Matriks hak minimum per proses.

Sumber kebenaran tunggal untuk tes ST-38, ST-39, dan ST-40. ``0002_roles.sql``
menuliskan GRANT yang sama dalam SQL; tes integrasi membandingkan keadaan
efektif di database dengan matriks di sini, dua arah:

* objek aplikasi di database tanpa baris matriks -> gagal
* baris matriks tanpa objek di database -> gagal

Modul ini murni data: tidak menyentuh database, jaringan, atau jam.

Catatan turunan (dikonfirmasi operator, 2 Nov 2026). Arsitektur v1.1 bagian
3.3 menandai beberapa UPDATE dengan ``U*`` ("per kolom sesuai transisi") tapi
hanya memberi satu contoh eksplisit, yaitu executor pada ``executions``.
Daftar kolom untuk ``decisions`` (guard dan approver), ``settlements``
(watcher), dan ``reconciliations`` (reconciler) di bawah diturunkan dari alur
di Arsitektur bagian 7, 10, dan 11, lalu dikunci. ``active_mandates`` dan
``watcher_state`` ditulis ``U`` tanpa bintang di matriks, jadi keduanya
mendapat UPDATE tingkat tabel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: Proses runtime yang punya role database sendiri.
PROCESSES: Final[tuple[str, ...]] = ("guard", "approver", "executor", "watcher", "reconciler")

#: Role grup NOLOGIN. Pemegang hak; dibuat ``guard db bootstrap-roles``.
GROUP_ROLES: Final[dict[str, str]] = {p: f"{p}_rw" for p in PROCESSES}

#: Role LOGIN per proses, anggota role grup masing-masing.
LOGIN_ROLES: Final[dict[str, str]] = {p: f"{p}_app" for p in PROCESSES}

#: Pemilik semua objek skema. Bukan superuser (Arsitektur v1.1 bagian 3.1).
OWNER_ROLE: Final = "migrator_owner"

#: Skema tempat semua objek aplikasi berada.
APP_SCHEMA: Final = "public"

#: Tabel yang hanya boleh diakses pemilik. Tidak ada GRANT ke role mana pun.
OWNER_ONLY_TABLES: Final[frozenset[str]] = frozenset({"schema_migrations"})

#: Hak yang tidak boleh dimiliki role mana pun, di objek mana pun (ST-34).
FORBIDDEN_TABLE_PRIVILEGES: Final[tuple[str, ...]] = ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


@dataclass(frozen=True)
class TableGrant:
    """Hak satu role grup pada satu tabel.

    ``update_columns`` punya tiga bentuk:

    * ``None``   -> tanpa UPDATE
    * ``()``     -> UPDATE tingkat tabel (semua kolom)
    * tuple isi  -> UPDATE hanya pada kolom yang disebut
    """

    select: bool = False
    insert: bool = False
    update_columns: tuple[str, ...] | None = None

    @property
    def has_update(self) -> bool:
        return self.update_columns is not None

    @property
    def table_level_update(self) -> bool:
        return self.update_columns == ()

    def privileges(self) -> tuple[str, ...]:
        """Hak tingkat tabel yang diharapkan ``has_table_privilege`` benar."""
        names = []
        if self.select:
            names.append("SELECT")
        if self.insert:
            names.append("INSERT")
        if self.table_level_update:
            names.append("UPDATE")
        return tuple(names)


_S = TableGrant(select=True)
_SI = TableGrant(select=True, insert=True)
_I = TableGrant(insert=True)


#: Matriks hak tabel: nama tabel -> proses -> hak. Proses yang tidak terdaftar
#: pada sebuah tabel tidak punya hak apa pun di tabel itu.
TABLE_MATRIX: Final[dict[str, dict[str, TableGrant]]] = {
    # mandates: append-only, dijaga trigger. Tidak ada UPDATE untuk siapa pun.
    "mandates": {
        "guard": _SI,
        "approver": _S,
        "executor": _S,
        "reconciler": _S,
    },
    # active_mandates: Guard menimpa barisnya saat start (UPSERT), jadi UPDATE
    # tingkat tabel. Matriks menulis "U" tanpa bintang.
    "active_mandates": {
        "guard": TableGrant(select=True, insert=True, update_columns=()),
        "approver": _S,
        "executor": _S,
    },
    "decisions": {
        # Guard: hanya kedaluwarsa lazy (needs_human -> expired).
        "guard": TableGrant(
            select=True,
            insert=True,
            update_columns=("status", "reason_code", "decided_at"),
        ),
        # Approver: approve, reject, dan kedaluwarsa lazy.
        "approver": TableGrant(
            select=True,
            update_columns=(
                "status",
                "reason_code",
                "decided_at",
                "approved_at",
                "valid_until",
                "envelope",
            ),
        ),
        "executor": _S,
        "reconciler": _S,
    },
    "executions": {
        "guard": _S,
        "approver": _S,
        # Daftar eksplisit di Arsitektur v1.1 bagian 3.3.
        "executor": TableGrant(
            select=True,
            insert=True,
            update_columns=("status", "broadcast_at", "updated_at"),
        ),
        "reconciler": _S,
    },
    "settlements": {
        # Watcher: pending -> final/reorged, dan settlement_moved (blok berubah).
        "watcher": TableGrant(
            select=True,
            insert=True,
            update_columns=(
                "status",
                "block_number",
                "block_hash",
                "finalized_at",
                "last_verified_at",
            ),
        ),
        "reconciler": _S,
    },
    "reconciliations": {
        # Reconciler: satu baris per subjek, di-upsert.
        "reconciler": TableGrant(
            select=True,
            insert=True,
            update_columns=("state", "detail", "updated_at"),
        ),
    },
    # watcher_state: posisi pemindaian. Matriks menulis "U" tanpa bintang.
    "watcher_state": {
        "watcher": TableGrant(select=True, insert=True, update_columns=()),
    },
    # audit_log: append-only. Semua proses bisa INSERT; tidak ada UPDATE/DELETE
    # untuk siapa pun (ST-34).
    "audit_log": {
        "guard": _SI,
        "approver": _SI,
        "executor": _I,
        "watcher": _I,
        "reconciler": _SI,
    },
}

#: Sequence -> proses yang punya USAGE. Hanya USAGE, tidak pernah SELECT atau
#: UPDATE: role aplikasi tidak boleh menggeser sequence selain lewat INSERT.
SEQUENCE_MATRIX: Final[dict[str, tuple[str, ...]]] = {
    "audit_log_id_seq": PROCESSES,
    "executions_id_seq": ("executor",),
    "settlements_id_seq": ("watcher",),
    "reconciliations_id_seq": ("reconciler",),
}

#: Proses yang butuh USAGE pada skema aplikasi. Semua role runtime butuh,
#: diberikan oleh bootstrap (schema-level bukan wewenang migrator).
SCHEMA_USAGE: Final[tuple[str, ...]] = PROCESSES


def all_matrix_tables() -> frozenset[str]:
    """Semua tabel yang punya baris matriks, termasuk yang khusus pemilik."""
    return frozenset(TABLE_MATRIX) | OWNER_ONLY_TABLES


def all_matrix_sequences() -> frozenset[str]:
    """Semua sequence yang punya baris matriks."""
    return frozenset(SEQUENCE_MATRIX)


def grant_for(table: str, process: str) -> TableGrant:
    """Hak ``process`` pada ``table``. Tanpa baris berarti tanpa hak."""
    return TABLE_MATRIX.get(table, {}).get(process, TableGrant())


def expected_update_columns(table: str, process: str) -> tuple[str, ...] | None:
    """Kolom yang boleh di-UPDATE, atau None bila tanpa UPDATE."""
    return grant_for(table, process).update_columns
