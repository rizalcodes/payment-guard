"""Fixture integrasi: Postgres 16 dari docker compose project ``payment-guard``.

Aturan yang dijaga di sini:

* Database tes **selalu** di port 5434, project compose ``payment-guard``.
  Tidak ada fallback ke database lain. Bila compose belum jalan, tes integrasi
  gagal dengan pesan yang menyebutkan perintahnya, bukan diam-diam memakai
  Postgres project lain di mesin yang sama.
* Setiap tes mendapat database baru **dan** keadaan role yang bersih. Role
  Postgres bersifat cluster-wide, bukan per database, jadi role juga harus
  dibersihkan supaya tes idempotensi bootstrap bermakna.
* ``.env`` proyek tidak pernah disentuh. Bootstrap dalam tes selalu menulis ke
  ``tmp_path``.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from payment_guard.bootstrap import ENV_KEY_FOR_PROCESS, MIGRATOR_ENV_KEY, run_bootstrap_roles
from payment_guard.config import MIGRATOR_ENV_FILENAME, RUNTIME_ENV_FILENAME, read_env_file
from payment_guard.migrate import migrations_dir, run_migrate
from payment_guard.privileges import GROUP_ROLES, LOGIN_ROLES, OWNER_ROLE, PROCESSES

#: URL admin untuk tes. Cocok dengan docker-compose.yml project payment-guard.
#: Bukan rahasia: database pengembangan lokal yang terikat ke 127.0.0.1.
DEFAULT_TEST_ADMIN_URL = (
    "postgresql://guard_admin:devonly_not_a_secret@127.0.0.1:5434/payment_guard"
)

#: Variabel untuk menimpa URL admin tes, mis. di CI.
TEST_ADMIN_URL_ENV = "PAYMENT_GUARD_TEST_ADMIN_DB_URL"

#: Awalan nama database tes. Dipakai juga untuk membersihkan sisa run lama.
TEST_DB_PREFIX = "pgtest_"

COMPOSE_HINT = (
    "Postgres tes tidak bisa dihubungi di 127.0.0.1:5434.\n"
    "Jalankan dari akar repo:  docker compose up -d\n"
    "Tes integrasi TIDAK akan jatuh ke database lain: container Postgres project\n"
    "lain di mesin ini (mis. fileshare di 5433) bukan pengganti yang sah."
)

#: Semua role yang dibuat bootstrap, dalam urutan aman untuk DROP.
ALL_MANAGED_ROLES = (
    *[LOGIN_ROLES[p] for p in PROCESSES],
    OWNER_ROLE,
    *[GROUP_ROLES[p] for p in PROCESSES],
)


def _admin_url() -> str:
    return os.environ.get(TEST_ADMIN_URL_ENV, "").strip() or DEFAULT_TEST_ADMIN_URL


def _replace_dbname(url: str, dbname: str) -> str:
    base, _, _ = url.rpartition("/")
    return f"{base}/{dbname}"


@pytest.fixture(scope="session")
def admin_url() -> str:
    """URL admin ke cluster tes. Gagal keras bila compose belum jalan."""
    url = _admin_url()
    try:
        with psycopg.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("SELECT current_setting('server_version_num')::int")
            row = cur.fetchone()
            assert row is not None
            version = row[0]
    except psycopg.OperationalError as exc:
        pytest.fail(f"{COMPOSE_HINT}\n\ndetail: {str(exc).strip().splitlines()[0]}")
    if version < 160000 or version >= 170000:
        pytest.fail(
            f"tes ini mengandaikan Postgres 16 (server_version_num={version}). "
            "docker-compose.yml memin versinya; periksa container mana yang terhubung."
        )
    return url


@pytest.fixture
def maintenance(admin_url: str) -> Iterator[psycopg.Connection]:
    """Koneksi admin autocommit untuk CREATE/DROP DATABASE dan DROP ROLE."""
    with psycopg.connect(admin_url, autocommit=True) as conn:
        yield conn


def _drop_leftover_test_databases(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE %s",
            (f"{TEST_DB_PREFIX}%",),
        )
        names = [row[0] for row in cur.fetchall()]
        for name in names:
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def _drop_managed_roles(conn: psycopg.Connection) -> None:
    """Hapus role Payment Guard bila ada, sejauh bisa dihapus.

    Harus dipanggil setelah semua database tes dihapus: Postgres menolak DROP
    ROLE selama role masih punya hak di database mana pun.

    Role yang **tidak bisa** dihapus dilewati, bukan menggagalkan tes. Kasus
    nyatanya: operator mengikuti README (``guard db bootstrap-roles`` lalu
    ``guard db migrate``) sehingga database ``payment_guard`` berisi skema yang
    dimiliki ``migrator_owner``. Role itu cluster-wide, jadi dari sini ia tidak
    bisa dihapus, dan memaksanya berarti menghapus objek di database milik
    operator. Yang penting untuk tes bukan role-nya absen, melainkan database
    tesnya baru dan bootstrap-nya idempoten; keduanya tetap terpenuhi.
    """
    with conn.cursor() as cur:
        for role in ALL_MANAGED_ROLES:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            if cur.fetchone() is None:
                continue
            try:
                cur.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
            except psycopg.errors.DependentObjectsStillExist:
                # Koneksi ini autocommit, jadi tidak ada transaksi yang perlu
                # dibatalkan dan role berikutnya tetap bisa dicoba.
                continue


def _surviving_managed_roles(conn: psycopg.Connection) -> list[str]:
    """Role terkelola yang masih ada setelah pembersihan."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
            (list(ALL_MANAGED_ROLES),),
        )
        return [row[0] for row in cur.fetchall()]


@pytest.fixture
def pristine_db(maintenance: psycopg.Connection, admin_url: str) -> Iterator[str]:
    """Database baru dan keadaan role bersih. Mengembalikan URL admin ke database itu.

    Dibersihkan setelah tes, termasuk role, supaya tes berikutnya benar-benar
    mulai dari nol.
    """
    _drop_leftover_test_databases(maintenance)
    _drop_managed_roles(maintenance)

    dbname = f"{TEST_DB_PREFIX}{secrets.token_hex(8)}"
    with maintenance.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    try:
        yield _replace_dbname(admin_url, dbname)
    finally:
        with maintenance.cursor() as cur:
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname))
            )
        _drop_managed_roles(maintenance)


@pytest.fixture
def pristine_roles(pristine_db: str, maintenance: psycopg.Connection) -> str:
    """``pristine_db`` yang juga menuntut tidak ada role terkelola tersisa.

    Sebagian kecil tes memeriksa jalur "role belum ada": bootstrap pertama,
    penolakan yang tidak boleh meninggalkan jejak, dan penjaga di
    ``0002_roles.sql`` yang harus gagal keras tanpa role grup. Tes itu hanya
    bermakna di cluster yang benar-benar bersih.

    Cluster pengembangan sering tidak bersih, dan bukan karena kesalahan:
    begitu operator mengikuti README (``bootstrap-roles`` lalu ``migrate``),
    database ``payment_guard`` berisi skema beserta hak untuk role yang sama.
    Role Postgres cluster-wide, jadi dari sini role itu tidak bisa dihapus
    tanpa ikut menghapus objek milik operator. Dalam keadaan itu tes dilewati
    dengan alasan yang menyebutkan penyebabnya, bukan gagal dan bukan diam-diam
    lolos.
    """
    leftovers = _surviving_managed_roles(maintenance)
    if leftovers:
        pytest.skip(
            "cluster ini memakai role terkelola di luar database tes: "
            f"{', '.join(leftovers)}. Penyebab lazimnya database `payment_guard` "
            "sudah dimigrasi mengikuti README, sehingga role-nya tidak bisa "
            "dihapus dari sini. Jalankan tes ini di cluster bersih (CI), atau "
            "pakai database lain untuk demo lokal."
        )
    return pristine_db


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    """File .env runtime khusus tes.

    ``.env`` dan ``.env.migrator`` milik proyek tidak pernah ditulis dari tes.
    """
    return tmp_path / RUNTIME_ENV_FILENAME


@pytest.fixture
def migrator_env_file(tmp_path: Path) -> Path:
    """File .env.migrator khusus tes."""
    return tmp_path / MIGRATOR_ENV_FILENAME


@pytest.fixture
def bootstrapped(pristine_db: str, env_file: Path, migrator_env_file: Path) -> dict[str, str]:
    """Jalankan bootstrap-roles pada database bersih.

    Mengembalikan gabungan isi kedua file env, dipakai tes sebagai kamus
    nama variabel -> URL. Penggabungan di sini hanya untuk kenyamanan tes;
    di produksi kedua file tetap terpisah, dan ada tes khusus yang memastikan
    MIGRATOR_DB_URL tidak pernah mendarat di ``.env``.
    """
    run_bootstrap_roles(pristine_db, env_file, migrator_env_file)

    runtime = read_env_file(env_file)
    migrator = read_env_file(migrator_env_file)

    missing_runtime = [k for k in ENV_KEY_FOR_PROCESS.values() if k not in runtime]
    assert not missing_runtime, f"bootstrap tidak menulis ke .env: {missing_runtime}"
    assert MIGRATOR_ENV_KEY in migrator, "bootstrap tidak menulis ke .env.migrator"

    return {**runtime, **migrator}


@pytest.fixture
def migrated(bootstrapped: dict[str, str]) -> dict[str, str]:
    """Database yang sudah di-bootstrap dan dimigrasi penuh (0001 dan 0002)."""
    applied = run_migrate(bootstrapped[MIGRATOR_ENV_KEY], migrations_dir())
    assert [f.label for f in applied] == ["0001_init.sql", "0002_roles.sql"]
    return bootstrapped


# Fixture khusus Guard (M3) hidup di berkas sendiri supaya conftest ini tetap
# terbaca. Impor bintang dipakai dengan sengaja: pytest hanya menemukan
# fixture yang ada di namespace conftest.
# Fixture khusus Executor (M5) juga hidup di berkas sendiri. Ia TIDAK memakai
# `pristine_db`: lihat penjelasan di kepala `conftest_executor.py` soal mengapa
# rantai fixture di atas merusak `.env` operator dan mengapa M5 tidak
# membutuhkannya.
from conftest_executor import *  # noqa: E402, F403
from conftest_guard import *  # noqa: E402, F403
