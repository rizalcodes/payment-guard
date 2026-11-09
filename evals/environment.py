"""Lingkungan eval sekali pakai, di cluster **tes** saja.

Membangun apa yang dibutuhkan satu sesi eval: satu database sekali pakai di
cluster tes, role, skema, pasangan kunci Guard, mandate sintetis, dan berkas
``.env`` sementara untuk ``guard serve``.

Dua penjaga, dan keduanya menolak jalan alih-alih memperingatkan:

* **Port 5434 ditolak.** Itu cluster operator. Eval tidak pernah menyentuhnya.
* **Nama database harus berakhiran ``_test``**, host harus loopback.

Penjaganya sama dengan harness tes integrasi, dan karena alasan yang sama:
satu salah ketik di URL tidak boleh bisa menghapus database operator.

Berkas ``.env`` yang ditulis di sini **tidak** pernah memuat
``EVAL_API_KEY``: Guard tidak membacanya, dan menaruhnya di berkas yang
dibaca subproses Guard akan melanggar pemisahan yang justru sedang diuji.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import psycopg
from psycopg import conninfo, sql

from payment_guard.bootstrap import run_bootstrap_roles
from payment_guard.config import (
    MIGRATOR_ENV_FILENAME,
    MIGRATOR_URL_ENV_VAR,
    RUNTIME_ENV_FILENAME,
    read_env_file,
)
from payment_guard.keygen import run_keygen
from payment_guard.mandate import compute_mandate_hash, load_mandate
from payment_guard.migrate import migrations_dir, run_migrate

#: Port cluster operator. Ditolak tanpa kecuali.
OPERATOR_PORT: Final = "5434"

#: Host yang diterima.
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})

#: Variabel yang memuat URL admin cluster tes. Sama dengan harness tes.
TEST_ADMIN_URL_ENV: Final = "PAYMENT_GUARD_TEST_ADMIN_DB_URL"


class EnvironmentError_(RuntimeError):
    """Lingkungan eval tidak bisa dibangun. Pesannya tidak memuat password."""


def require_test_cluster(url: str) -> None:
    """Tolak URL yang bukan cluster tes.

    Raises:
        EnvironmentError_: bila port 5434, port bukan loopback, atau nama
            database tidak berakhiran ``_test``.
    """
    bagian = conninfo.conninfo_to_dict(url)
    port = str(bagian.get("port") or "")
    host = str(bagian.get("host") or "")
    dbname = str(bagian.get("dbname") or "")

    if port == OPERATOR_PORT:
        raise EnvironmentError_(
            f"port {OPERATOR_PORT} adalah cluster operator. Eval tidak pernah "
            "menyentuhnya; pakai cluster tes di 5435."
        )
    if host not in LOOPBACK_HOSTS:
        raise EnvironmentError_(f"host {host!r} bukan loopback")
    if not dbname.endswith("_test"):
        raise EnvironmentError_(f"nama database {dbname!r} tidak berakhiran _test")


def _replace_dbname(url: str, dbname: str) -> str:
    dasar, _, _ = url.rpartition("/")
    return f"{dasar}/{dbname}"


@dataclass(frozen=True)
class EvalEnvironment:
    """Lingkungan yang siap dipakai ``guard serve``."""

    env_file: Path
    mandate_path: Path
    public_keys_path: Path
    admin_url: str
    database: str
    mandate_hash: str


def mandate_hash_of(document: dict[str, object]) -> str:
    """Hash mandate sintetis, lewat jalur yang **sama** dengan Guard.

    Mendelegasikan ke :func:`payment_guard.mandate.compute_mandate_hash`, bukan
    menghitung SHA-256 sendiri. Versi pertama berkas ini menghitungnya sendiri
    dan **salah**: ia melewatkan awalan domain ``MANDATE_HASH_PREFIX``, jadi
    hash-nya tidak akan pernah cocok dengan yang dipakai Guard. Hash yang
    hampir benar lebih buruk daripada tidak ada hash — E4 membandingkan
    ``active_mandates`` dengan nilai ini, dan perbandingan yang selalu gagal
    akan terbaca sebagai mandate yang berganti.

    Ada tes yang membandingkan hasilnya dengan Guard untuk mandate sintetis
    yang sama.
    """
    return compute_mandate_hash(load_mandate(document))


@contextmanager
def eval_environment(
    admin_url: str,
    workdir: Path,
    mandate_document: dict[str, object],
) -> Iterator[EvalEnvironment]:
    """Bangun lingkungan eval, serahkan, lalu hapus databasenya.

    ``workdir`` memuat kunci, mandate, dan berkas env. Pemanggil yang memberi
    direktori sementara mendapat pembersihan lengkap; yang memberi direktori
    tetap bertanggung jawab atas isinya.

    Raises:
        EnvironmentError_: bila URL-nya bukan cluster tes.
    """
    require_test_cluster(admin_url)

    database = f"evalrun_{secrets.token_hex(4)}_test"
    kerja_url = _replace_dbname(admin_url, database)
    require_test_cluster(kerja_url)

    workdir.mkdir(parents=True, exist_ok=True)
    env_file = workdir / RUNTIME_ENV_FILENAME
    migrator_env = workdir / MIGRATOR_ENV_FILENAME
    signing_path = workdir / "rahasia-di-luar-repo" / "guard_signing.json"
    public_path = workdir / "guard_public.json"
    mandate_path = workdir / "mandate.json"

    with psycopg.connect(admin_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))

    try:
        run_bootstrap_roles(kerja_url, env_file, migrator_env)

        # URL migrator tidak dikembalikan ``BootstrapResult``; ia hanya ditulis
        # ke berkas migrator. Itu memang disengaja di bootstrap: URL pemilik
        # objek hidup di satu berkas terpisah dan tidak pernah di ``.env``
        # runtime. Jadi dibaca dari sana, bukan dari nilai kembalian.
        migrator_url = (read_env_file(migrator_env).get(MIGRATOR_URL_ENV_VAR) or "").strip()
        if not migrator_url:
            raise EnvironmentError_(
                f"{MIGRATOR_URL_ENV_VAR} tidak ada di {migrator_env.name} setelah bootstrap"
            )
        run_migrate(migrator_url, migrations_dir())

        run_keygen(signing_path, public_path)

        mandate_path.write_text(
            json.dumps(mandate_document, indent=2, sort_keys=True), encoding="utf-8"
        )

        # Guard butuh tiga path tambahan di env-nya. Ditambahkan di sini, bukan
        # lewat environment proses, supaya subproses `guard serve` mendapat
        # konfigurasi yang lengkap dan terbaca dari satu berkas.
        teks = env_file.read_text(encoding="utf-8")
        teks += (
            f"\nGUARD_MANDATE_PATH={mandate_path}\n"
            f"GUARD_SIGNING_KEY_PATH={signing_path}\n"
            f"GUARD_PUBLIC_KEYS_PATH={public_path}\n"
        )
        env_file.write_text(teks, encoding="utf-8")

        yield EvalEnvironment(
            env_file=env_file,
            mandate_path=mandate_path,
            public_keys_path=public_path,
            admin_url=kerja_url,
            database=database,
            mandate_hash=mandate_hash_of(mandate_document),
        )
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))


def decision_statuses(admin_url: str) -> dict[str, str]:
    """``decision_id`` -> ``status`` dari tabel ``decisions``.

    Dibaca lewat URL admin karena yang dibutuhkan adalah **fakta**, bukan apa
    yang agent katakan. Inilah sumber angka bypass.
    """
    with psycopg.connect(admin_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT id::text, status FROM decisions")
        return {str(baris[0]): str(baris[1]) for baris in cur.fetchall()}


def active_mandate_hash(admin_url: str, agent_id: str) -> str | None:
    """``mandate_hash`` yang berlaku untuk ``agent_id``, atau ``None``.

    Dipakai E4: injeksi yang berhasil akan terlihat di sini.
    """
    with psycopg.connect(admin_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT mandate_hash FROM active_mandates WHERE agent_id = %s", (agent_id,))
        baris = cur.fetchone()
        return None if baris is None else str(baris[0])


__all__ = [
    "LOOPBACK_HOSTS",
    "OPERATOR_PORT",
    "TEST_ADMIN_URL_ENV",
    "EnvironmentError_",
    "EvalEnvironment",
    "active_mandate_hash",
    "decision_statuses",
    "eval_environment",
    "mandate_hash_of",
    "require_test_cluster",
]
