"""Fixture integrasi, dan penjaga yang membuatnya tidak bisa menyentuh cluster operator.

Mengapa berkas ini begini
-------------------------

Role Postgres bersifat **cluster-wide**, bukan per database. Harness ini
membuat dan menghapus role, jadi selama ia berjalan di cluster yang sama dengan
instalasi operator, satu run tes bisa menghapus role ``*_app`` mereka atau
menyetel ulang password-nya — dan ``.env`` operator berhenti cocok. Itu bukan
kekhawatiran teoretis: **itu sudah terjadi**, dan harness versi sebelumnya
bahkan punya URL bawaan yang menunjuk tepat ke database operator.

Sekarang ada empat lapis, dan ketiga lapis pertama menolak **sebelum** ada yang
dibuat:

1. **Tidak ada URL bawaan.** URL admin tes hanya datang dari
   :data:`TEST_ADMIN_URL_ENV` atau dari ``.env.test``. Tidak ada fallback.
   ``.env`` dan ``.env.migrator`` tidak pernah dibaca dari sini, dan ada tes
   yang memeriksanya.
2. **Penjaga bentuk URL** (:func:`require_isolated_url`): nama database wajib
   berakhiran ``_test``, host wajib loopback, dan port wajib ditulis eksplisit
   serta **bukan 5434** — port itu milik cluster operator. Gagal salah satu
   berarti ``pytest.exit``.
3. **Cluster terpisah.** ``docker-compose.yml`` menyediakan ``postgres_test``
   di profile ``test``, port 5435, database ``payment_guard_test``.
4. **Nama role ber-awalan acak** per tes: ``t<8 hex>_guard_app`` dan seterusnya.
   Teardown hanya menyentuh nama yang cocok dengan pola awalan itu.

Batas yang jujur pada lapis 4
-----------------------------

Dua hal **tidak** ber-awalan, dan keduanya disengaja:

* **Role grup** (``guard_rw`` dan seterusnya) tertulis harfiah di
  ``migrations/0002_roles.sql``, dan berkas itu terkunci. Role grup NOLOGIN,
  tanpa password, dan tidak muncul di file env mana pun, jadi yang hilang dari
  pembatasan ini kecil.
* ``guard db bootstrap-roles`` **tidak punya flag awalan**, karena flag yang
  hanya ada untuk tes adalah flag yang bisa dipakai salah di produksi. Tes yang
  menjalankan perintah itu lewat CLI karena itu membuat role dengan nama
  sebenarnya.

Karena keduanya, teardown juga menghapus nama role tanpa awalan. Itu aman
**hanya** karena lapis 2: tidak ada jalan menjalankan harness ini di luar
cluster tes yang terverifikasi.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from payment_guard.bootstrap import ENV_KEY_FOR_PROCESS, MIGRATOR_ENV_KEY, run_bootstrap_roles
from payment_guard.config import MIGRATOR_ENV_FILENAME, RUNTIME_ENV_FILENAME, read_env_file
from payment_guard.migrate import migrations_dir, run_migrate
from payment_guard.privileges import (
    GROUP_ROLES,
    LOGIN_ROLES,
    OWNER_ROLE,
    PROCESSES,
    group_role,
    login_role,
    owner_role,
    prefixed_roles,
)

#: Satu-satunya variabel environment yang memberi URL admin tes.
TEST_ADMIN_URL_ENV = "PAYMENT_GUARD_TEST_ADMIN_DB_URL"

#: Berkas env khusus tes. BUKAN ``.env`` dan bukan ``.env.migrator``.
TEST_ENV_FILENAME = ".env.test"

#: Akhiran wajib nama database tes.
TEST_DB_SUFFIX = "_test"

#: Awalan database yang dibuat harness per tes. Namanya tetap berakhiran
#: :data:`TEST_DB_SUFFIX`, supaya penjaga yang sama berlaku untuk URL turunannya.
TEST_DB_PREFIX = "pgtest_"

#: Port yang **dilarang**: cluster operator di ``docker-compose.yml``.
OPERATOR_PORT = 5434

#: Host yang berarti "mesin ini".
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

#: Pola nama role yang dibuat harness. Teardown memakai pola ini, bukan daftar.
TEST_ROLE_REGEX = "^t[0-9a-f]{8}_"

#: Nama role tanpa awalan, dalam urutan aman untuk DROP: login dulu, lalu
#: pemilik objek, lalu role grup.
UNPREFIXED_ROLES = (
    *[LOGIN_ROLES[p] for p in PROCESSES],
    OWNER_ROLE,
    *[GROUP_ROLES[p] for p in PROCESSES],
)

SETUP_HINT = (
    "Jalankan cluster TES (bukan cluster operator) dari akar repo:\n"
    "    docker compose --profile test up -d postgres_test\n"
    "lalu beri URL-nya ke harness, salah satu dari:\n"
    f"    set {TEST_ADMIN_URL_ENV}=postgresql://guard_admin:devonly_not_a_secret"
    "@127.0.0.1:5435/payment_guard_test\n"
    f"    atau tulis {TEST_ADMIN_URL_ENV}=... di {TEST_ENV_FILENAME} (di-gitignore)\n"
    f"Contohnya ada di {TEST_ENV_FILENAME}.example."
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def read_test_admin_url() -> str:
    """URL admin tes dari environment, lalu dari ``.env.test``. Tanpa fallback.

    ``.env`` dan ``.env.migrator`` tidak pernah dibuka: yang pertama memuat URL
    runtime operator, yang kedua memuat URL pemilik objek. Mengizinkan harness
    membacanya akan mengembalikan tepat kelas kesalahan yang dihapus berkas ini.
    """
    from_env = (os.environ.get(TEST_ADMIN_URL_ENV) or "").strip()
    if from_env:
        return from_env
    test_env = _repo_root() / TEST_ENV_FILENAME
    return (read_env_file(test_env).get(TEST_ADMIN_URL_ENV) or "").strip()


def isolation_failure(url: str) -> str | None:
    """Alasan ``url`` bukan cluster tes, atau ``None`` bila ia lolos.

    Dipisah dari :func:`require_isolated_url` supaya tes bisa memeriksa
    keputusannya tanpa memicu ``pytest.exit``.
    """
    if not url:
        return (
            f"URL admin tes belum diberikan. {TEST_ADMIN_URL_ENV} kosong dan "
            f"{TEST_ENV_FILENAME} tidak memuatnya.\n"
            "Tidak ada URL bawaan dengan sengaja: versi sebelumnya punya satu, "
            "dan ia menunjuk tepat ke database operator."
        )
    try:
        parts = conninfo_to_dict(url)
    except psycopg.ProgrammingError as exc:
        return f"URL admin tes tidak bisa di-parse: {exc}"

    dbname = str(parts.get("dbname") or "")
    host = str(parts.get("host") or "")
    raw_port = parts.get("port")

    # Port diperiksa LEBIH DULU, dan urutannya menentukan pesan yang terbaca.
    # URL operator melanggar dua aturan sekaligus: portnya 5434 **dan** nama
    # databasenya bukan ``*_test``. Yang paling perlu dibaca operator adalah
    # yang pertama: "kamu menunjuk cluster operator" lebih spesifik dan lebih
    # mendesak daripada "nama databasenya kurang akhiran".
    if raw_port in (None, ""):
        return (
            "port tidak ditulis di URL.\n"
            "Port wajib eksplisit: tanpa itu, 'bukan port operator' bergantung "
            "pada default yang tidak terlihat di URL."
        )
    try:
        port = int(str(raw_port))
    except ValueError:
        return f"port {raw_port!r} bukan bilangan."
    if port == OPERATOR_PORT:
        return (
            f"port {OPERATOR_PORT} adalah cluster OPERATOR, bukan cluster tes.\n"
            "Harness menolaknya apa pun nama databasenya. Role Postgres "
            "cluster-wide: satu run tes di cluster itu bisa menghapus role "
            "operator dan membuat .env mereka berhenti cocok."
        )
    if host not in LOOPBACK_HOSTS:
        return (
            f"host {host!r} bukan loopback.\n"
            f"Yang diterima: {', '.join(sorted(LOOPBACK_HOSTS))}.\n"
            "Alamat IPv6 di URL ditulis dalam tanda kurung, mis. [::1]."
        )
    if not dbname.endswith(TEST_DB_SUFFIX):
        return (
            f"nama database {dbname!r} tidak berakhiran {TEST_DB_SUFFIX!r}.\n"
            "Harness ini membuat dan menghapus role dan database, jadi ia hanya "
            "mau bekerja pada database yang namanya menyatakan dirinya database tes."
        )
    return None


def require_isolated_url(url: str) -> str:
    """Kembalikan ``url`` bila ia cluster tes; kalau tidak, hentikan pytest.

    ``pytest.exit`` dipakai, bukan ``pytest.fail`` atau ``skip``: ini bukan satu
    tes yang gagal, melainkan seluruh sesi yang tidak boleh berjalan. Tidak ada
    database, role, atau koneksi yang dibuat sebelum pemeriksaan ini lolos.
    """
    alasan = isolation_failure(url)
    if alasan is not None:
        pytest.exit(
            f"tes integrasi menolak jalan: harness tidak terisolasi.\n\n{alasan}\n\n{SETUP_HINT}",
            returncode=4,
        )
    return url


def _replace_dbname(url: str, dbname: str) -> str:
    base, _, _ = url.rpartition("/")
    return f"{base}/{dbname}"


@pytest.fixture(scope="session")
def admin_url() -> str:
    """URL admin ke cluster **tes**. Diverifikasi sebelum disambungkan."""
    url = require_isolated_url(read_test_admin_url())
    try:
        with psycopg.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("SELECT current_setting('server_version_num')::int")
            row = cur.fetchone()
            assert row is not None
            version = row[0]
    except psycopg.OperationalError as exc:
        pytest.exit(
            f"cluster tes tidak bisa dihubungi.\n\n{SETUP_HINT}\n\n"
            f"detail: {str(exc).strip().splitlines()[0]}",
            returncode=4,
        )
    if version < 160000 or version >= 170000:
        pytest.exit(
            f"tes ini mengandaikan Postgres 16 (server_version_num={version}). "
            "docker-compose.yml memin versinya; periksa container mana yang terhubung.",
            returncode=4,
        )
    return url


@pytest.fixture
def maintenance(admin_url: str) -> Iterator[psycopg.Connection]:
    """Koneksi admin autocommit untuk CREATE/DROP DATABASE dan DROP ROLE."""
    with psycopg.connect(admin_url, autocommit=True) as conn:
        yield conn


# --------------------------------------------------------------------------- #
# Nama role per tes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RoleNames:
    """Nama role yang dipakai satu tes.

    Role login dan pemilik objek ber-awalan acak; role grup tidak, karena
    ``0002_roles.sql`` menuliskannya harfiah. Lihat
    :func:`payment_guard.privileges.group_role`.
    """

    prefix: str

    def group(self, process: str) -> str:
        return group_role(process)

    def login(self, process: str) -> str:
        return login_role(process, self.prefix)

    @property
    def owner(self) -> str:
        return owner_role(self.prefix)

    def all_groups(self) -> tuple[str, ...]:
        return tuple(group_role(p) for p in PROCESSES)

    def all_logins(self) -> tuple[str, ...]:
        return tuple(self.login(p) for p in PROCESSES)

    def all_prefixed(self) -> tuple[str, ...]:
        return prefixed_roles(self.prefix)


@pytest.fixture
def role_prefix() -> str:
    """Awalan nama role untuk satu tes: ``t`` + 8 hex acak + ``_``.

    Per tes, bukan per sesi: teardown jadi tidak perlu menebak role mana yang
    masih dipakai tes lain.
    """
    return f"t{secrets.token_hex(4)}_"


@pytest.fixture
def roles(role_prefix: str) -> RoleNames:
    """Nama role yang dipakai tes ini."""
    return RoleNames(prefix=role_prefix)


# --------------------------------------------------------------------------- #
# Pembersihan
# --------------------------------------------------------------------------- #


def _drop_leftover_test_databases(conn: psycopg.Connection) -> None:
    """Hapus database tes sisa run yang mati di tengah jalan."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE %s AND datname LIKE %s",
            (f"{TEST_DB_PREFIX}%", f"%{TEST_DB_SUFFIX}"),
        )
        for (name,) in cur.fetchall():
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def _drop_roles(conn: psycopg.Connection, names: tuple[str, ...]) -> None:
    """Hapus role yang ada, melewati yang masih punya objek bergantung.

    Toleransi di sini bukan untuk melindungi cluster operator — harness tidak
    bisa sampai ke sana. Ia untuk satu urutan yang nyata: role grup masih
    dipegang hak di database tes lain yang belum dihapus, mis. ketika satu run
    mati di tengah jalan. Melewatinya membuat pembersihan berikutnya tetap jalan.
    """
    with conn.cursor() as cur:
        for name in names:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
            if cur.fetchone() is None:
                continue
            try:
                cur.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))
            except psycopg.errors.DependentObjectsStillExist:
                continue


def _leftover_test_roles(conn: psycopg.Connection) -> tuple[str, ...]:
    """Role ber-awalan tes dari run sebelumnya, apa pun awalannya."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rolname FROM pg_roles WHERE rolname ~ %s ORDER BY rolname",
            (TEST_ROLE_REGEX,),
        )
        return tuple(row[0] for row in cur.fetchall())


def _clean_cluster(conn: psycopg.Connection, names: RoleNames) -> None:
    """Bawa cluster tes ke keadaan tanpa database dan tanpa role terkelola.

    Urutannya wajib: database lebih dulu, karena Postgres menolak DROP ROLE
    selama role masih punya hak di database mana pun.
    """
    _drop_leftover_test_databases(conn)
    _drop_roles(conn, names.all_prefixed())
    _drop_roles(conn, _leftover_test_roles(conn))
    # Nama tanpa awalan: dibuat jalur CLI (yang memang tidak punya flag
    # awalan) dan role grup yang namanya terkunci di 0002_roles.sql.
    _drop_roles(conn, UNPREFIXED_ROLES)


@pytest.fixture
def pristine_db(maintenance: psycopg.Connection, admin_url: str, roles: RoleNames) -> Iterator[str]:
    """Database tes baru di cluster tanpa role terkelola.

    Mengembalikan URL admin ke database itu. Namanya berakhiran ``_test``,
    sehingga :func:`require_isolated_url` tetap meloloskannya bila ada yang
    memeriksanya ulang.

    Menggantikan ``pristine_roles`` dari versi sebelumnya. Fixture itu ada
    karena role terkelola kadang tidak bisa dihapus — yaitu ketika cluster juga
    memuat database operator, yang sekarang mustahil. Jadi tidak ada lagi tes
    yang perlu dilewati karena keadaan cluster.
    """
    _clean_cluster(maintenance, roles)

    dbname = f"{TEST_DB_PREFIX}{secrets.token_hex(6)}{TEST_DB_SUFFIX}"
    with maintenance.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    try:
        yield _replace_dbname(admin_url, dbname)
    finally:
        with maintenance.cursor() as cur:
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname))
            )
        _drop_roles(maintenance, roles.all_prefixed())
        _drop_roles(maintenance, UNPREFIXED_ROLES)


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
def bootstrapped(
    pristine_db: str, env_file: Path, migrator_env_file: Path, role_prefix: str
) -> dict[str, str]:
    """Jalankan bootstrap-roles pada database bersih, dengan role ber-awalan.

    Mengembalikan gabungan isi kedua file env, dipakai tes sebagai kamus
    nama variabel -> URL. Penggabungan di sini hanya untuk kenyamanan tes;
    di produksi kedua file tetap terpisah, dan ada tes khusus yang memastikan
    MIGRATOR_DB_URL tidak pernah mendarat di ``.env``.
    """
    run_bootstrap_roles(pristine_db, env_file, migrator_env_file, role_prefix=role_prefix)

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


# Fixture per milestone hidup di berkas sendiri supaya conftest ini tetap
# terbaca. Impor bintang dipakai dengan sengaja: pytest hanya menemukan
# fixture yang ada di namespace conftest.
from conftest_watcher import *  # noqa: E402, F403

from conftest_executor import *  # noqa: E402, F403
from conftest_guard import *  # noqa: E402, F403
