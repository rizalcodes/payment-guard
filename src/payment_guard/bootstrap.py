"""``guard db bootstrap-roles`` — dijalankan admin, sekali.

Membuat lanskap role database yang dipakai semua proses lain
(Arsitektur v1.1 bagian 3), lalu menulis URL per proses ke file env.

Pembagian tugas dengan migrator disengaja dan tidak boleh dibalik:

* **bootstrap (admin)** -> ``CREATE ROLE``, keanggotaan grup, dan hak tingkat
  schema. Migrator tidak punya wewenang di tingkat itu.
* **``0002_roles.sql`` (migrator)** -> hanya hak tingkat tabel dan sequence.

Dua file env ditulis, dan pembagiannya juga disengaja:

* ``.env`` -> lima URL runtime.
* ``.env.migrator`` -> hanya ``MIGRATOR_DB_URL``, URL role pemilik objek.

Lihat :mod:`payment_guard.config` untuk alasan dan batasnya.

Aturan rahasia yang dipegang modul ini:

* URL admin dibaca dari environment variable sementara atau prompt tersembunyi
  (:func:`getpass.getpass`, tidak di-echo). Tidak pernah disimpan, tidak pernah
  dicetak, tidak pernah masuk file env mana pun.
* URL admin harus menunjuk host loopback, kecuali operator menyatakannya
  sendiri lewat ``--allow-remote-admin``. Bootstrap memegang wewenang tertinggi
  di cluster; menjalankannya terhadap host jauh tanpa sengaja adalah kesalahan
  yang murah dicegah.
* Password role dibuat acak dengan :mod:`secrets`. Tidak pernah dicetak ke
  terminal maupun log. Satu-satunya tempat ia mendarat adalah file env.
* SQL yang menyebut nama role memakai :mod:`psycopg.sql` composable, jadi
  quoting dikerjakan driver — bukan oleh f-string.
"""

from __future__ import annotations

import getpass
import ipaddress
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from urllib.parse import quote

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from payment_guard.config import (
    ADMIN_URL_ENV_VAR,
    MIGRATOR_URL_ENV_VAR,
    RUNTIME_URL_ENV_VARS,
    read_env_file,
    write_env_file,
)
from payment_guard.db import MigrationError, connect
from payment_guard.privileges import (
    APP_SCHEMA,
    GROUP_ROLES,
    PROCESSES,
    SCHEMA_USAGE,
    login_role,
    owner_role,
    require_role_prefix,
)

#: Panjang entropi password role, dalam byte sebelum pengkodean.
_PASSWORD_BYTES: Final = 32

#: Proses runtime -> nama variabel di ``.env``. Alias untuk keterbacaan;
#: definisinya ada di :mod:`payment_guard.config`.
ENV_KEY_FOR_PROCESS: Final[dict[str, str]] = RUNTIME_URL_ENV_VARS

#: Variabel untuk role pemilik objek. Ditulis ke ``.env.migrator``.
MIGRATOR_ENV_KEY: Final = MIGRATOR_URL_ENV_VAR

#: Nama host yang berarti "mesin ini" walau bukan alamat IP.
_LOCAL_HOSTNAMES: Final[frozenset[str]] = frozenset({"localhost"})


@dataclass
class BootstrapResult:
    """Ringkasan hasil bootstrap. Sengaja tidak memuat rahasia apa pun."""

    group_roles_created: list[str] = field(default_factory=list)
    group_roles_existing: list[str] = field(default_factory=list)
    login_roles_created: list[str] = field(default_factory=list)
    login_roles_reused: list[str] = field(default_factory=list)
    login_roles_password_reset: list[str] = field(default_factory=list)
    env_keys_written: list[str] = field(default_factory=list)
    migrator_env_keys_written: list[str] = field(default_factory=list)
    env_path: Path | None = None
    migrator_env_path: Path | None = None

    def summary_lines(self) -> list[str]:
        """Baris laporan untuk operator. Tidak memuat password atau URL."""
        lines = []
        if self.group_roles_created:
            lines.append(f"role grup dibuat      : {', '.join(self.group_roles_created)}")
        if self.group_roles_existing:
            lines.append(f"role grup sudah ada   : {', '.join(self.group_roles_existing)}")
        if self.login_roles_created:
            lines.append(f"role login dibuat     : {', '.join(self.login_roles_created)}")
        if self.login_roles_password_reset:
            lines.append(
                f"password di-set ulang : {', '.join(self.login_roles_password_reset)} "
                "(URL-nya belum tercatat)"
            )
        if self.login_roles_reused:
            lines.append(f"role login dipakai ulang: {', '.join(self.login_roles_reused)}")

        for path, keys in (
            (self.env_path, self.env_keys_written),
            (self.migrator_env_path, self.migrator_env_keys_written),
        ):
            if keys:
                lines.append(f"ditulis ke {path}: {', '.join(keys)}")
            else:
                lines.append(f"{path}: tidak ada perubahan")
        return lines


def read_admin_url(*, allow_prompt: bool = True) -> str:
    """Ambil URL admin dari environment sementara, atau prompt tersembunyi.

    URL ini tidak pernah ditulis ke ``.env`` dan tidak pernah dicetak.
    Security v1.1 I6.

    Raises:
        MigrationError: bila tidak tersedia.
    """
    url = os.environ.get(ADMIN_URL_ENV_VAR, "").strip()
    if url:
        return url
    if not allow_prompt:
        raise MigrationError(
            f"{ADMIN_URL_ENV_VAR} belum diisi dan prompt dimatikan. "
            "Berikan URL admin lewat environment variable sementara."
        )
    url = getpass.getpass(
        f"URL admin Postgres (tidak ditampilkan, tidak disimpan; atau set {ADMIN_URL_ENV_VAR}): "
    ).strip()
    if not url:
        raise MigrationError("URL admin kosong; bootstrap dibatalkan.")
    return url


def _new_password() -> str:
    """Password acak yang aman dipakai langsung di URL tanpa escape."""
    return secrets.token_urlsafe(_PASSWORD_BYTES)


def admin_hosts(admin_url: str) -> list[str]:
    """Host yang disebut ``admin_url``.

    libpq mengizinkan beberapa host dipisah koma. Daftar kosong berarti URL
    tidak menyebut host sama lain sekali, yaitu socket lokal atau default
    ``localhost``.
    """
    raw = conninfo_to_dict(admin_url).get("host")
    if not raw:
        return []
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def is_loopback_host(host: str) -> bool:
    """True bila ``host`` menunjuk mesin ini.

    Mencakup ``localhost``, seluruh ``127.0.0.0/8``, ``::1``, dan path socket
    Unix (libpq menandainya dengan awalan ``/`` atau ``@``).
    """
    if host.lower() in _LOCAL_HOSTNAMES:
        return True
    if host.startswith(("/", "@")):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def require_loopback_admin(admin_url: str, *, allow_remote: bool = False) -> None:
    """Tolak URL admin yang menunjuk host non-loopback.

    Bootstrap membuat role dan mengubah hak tingkat schema; menjalankannya
    terhadap cluster yang salah adalah kesalahan mahal yang murah dicegah.
    Operator yang memang bermaksud begitu memakai ``--allow-remote-admin``.

    Pesan error menyebut host yang ditolak, tapi **tidak pernah** URL-nya:
    URL memuat password.

    Raises:
        MigrationError: bila ada host yang bukan loopback dan ``allow_remote``
            tidak disetel.
    """
    if allow_remote:
        return
    remote = [host for host in admin_hosts(admin_url) if not is_loopback_host(host)]
    if remote:
        raise MigrationError(
            "URL admin menunjuk host yang bukan loopback: "
            + ", ".join(remote)
            + ".\nbootstrap-roles membuat role dan mengubah hak tingkat schema, "
            "jadi secara bawaan ia hanya mau menjalankan itu terhadap Postgres "
            "di mesin ini (127.0.0.0/8, ::1, localhost, atau socket lokal).\n"
            "Bila memang disengaja, ulangi dengan --allow-remote-admin."
        )


def build_url(admin_url: str, role: str, password: str) -> str:
    """Susun URL per proses dari host, port, dan database milik ``admin_url``.

    Hanya user dan password yang berbeda dari URL admin.
    """
    info = conninfo_to_dict(admin_url)
    host = str(info.get("host") or "127.0.0.1")
    port = str(info.get("port") or "5432")
    dbname = str(info.get("dbname") or role)
    return f"postgresql://{quote(role, safe='')}:{quote(password, safe='')}@{host}:{port}/{dbname}"


def _require_createrole(cur: psycopg.Cursor) -> None:
    """Pastikan role yang menyambung boleh membuat role lain. ST-41."""
    cur.execute(
        "SELECT rolsuper, rolcreaterole, current_user FROM pg_roles WHERE rolname = current_user"
    )
    row = cur.fetchone()
    if row is None:
        raise MigrationError("tidak bisa membaca atribut role saat ini dari pg_roles.")
    is_super, can_create_role, who = row
    if not (is_super or can_create_role):
        raise MigrationError(
            f"role '{who}' tidak punya CREATEROLE (dan bukan superuser), "
            "jadi tidak bisa menjalankan bootstrap-roles. "
            "Pakai URL admin yang punya CREATEROLE. "
            "Catatan: URL migrator memang TIDAK boleh punya CREATEROLE."
        )


def _role_exists(cur: psycopg.Cursor, name: str) -> bool:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    return cur.fetchone() is not None


def _create_group_role(cur: psycopg.Cursor, name: str) -> None:
    """Buat role grup NOLOGIN secara idempoten lewat blok DO.

    Blok DO dipakai (bukan cek-lalu-buat di Python) supaya pemeriksaan
    ``pg_roles`` dan ``CREATE ROLE`` berada dalam satu pernyataan server,
    sehingga dua bootstrap paralel tidak saling menabrak.
    """
    cur.execute(
        sql.SQL(
            """
            DO $do$
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {name_literal}) THEN
                CREATE ROLE {name_ident} NOLOGIN NOINHERIT NOCREATEDB NOCREATEROLE;
              END IF;
            END
            $do$;
            """
        ).format(name_literal=sql.Literal(name), name_ident=sql.Identifier(name))
    )
    # Pastikan atribut benar juga untuk role yang sudah ada sebelumnya.
    cur.execute(
        sql.SQL("ALTER ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION").format(
            sql.Identifier(name)
        )
    )


def _create_login_role(cur: psycopg.Cursor, name: str, password: str) -> None:
    """Buat role LOGIN dengan password acak, idempoten lewat blok DO."""
    cur.execute(
        sql.SQL(
            """
            DO $do$
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {name_literal}) THEN
                CREATE ROLE {name_ident} LOGIN INHERIT
                  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
                  PASSWORD {password_literal};
              END IF;
            END
            $do$;
            """
        ).format(
            name_literal=sql.Literal(name),
            name_ident=sql.Identifier(name),
            password_literal=sql.Literal(password),
        )
    )


def _set_password(cur: psycopg.Cursor, name: str, password: str) -> None:
    cur.execute(
        sql.SQL("ALTER ROLE {} LOGIN INHERIT PASSWORD {}").format(
            sql.Identifier(name), sql.Literal(password)
        )
    )


def _grant_membership(cur: psycopg.Cursor, member: str, group: str) -> None:
    """Jadikan ``member`` anggota ``group``. GRANT role bersifat idempoten."""
    cur.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(group), sql.Identifier(member)))


def _schema_privileges(cur: psycopg.Cursor, role_prefix: str) -> None:
    """Cabut hak PUBLIC lalu beri USAGE sesuai matriks. Security v1.1 E11, ST-40."""
    schema = sql.Identifier(APP_SCHEMA)
    cur.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(schema))

    # Migrator memiliki objek, jadi butuh CREATE selain USAGE.
    cur.execute(
        sql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO {}").format(
            schema, sql.Identifier(owner_role(role_prefix))
        )
    )
    for process in SCHEMA_USAGE:
        cur.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                schema, sql.Identifier(GROUP_ROLES[process])
            )
        )


def run_bootstrap_roles(
    admin_url: str,
    env_path: Path,
    migrator_env_path: Path,
    *,
    allow_remote_admin: bool = False,
    role_prefix: str = "",
) -> BootstrapResult:
    """Jalankan bootstrap. Idempoten: jalan kedua tidak mengubah keadaan.

    Lima URL runtime ditulis ke ``env_path``; URL pemilik objek ditulis ke
    ``migrator_env_path``. Keduanya dibaca lebih dulu, supaya jalan kedua
    mengenali URL yang sudah ada dan tidak merotasi password tanpa diminta.

    Password hanya dibuat untuk role login yang belum ada, atau yang URL-nya
    belum tercatat (password lama tidak bisa dibaca kembali dari Postgres,
    jadi satu-satunya cara memulihkan URL adalah menyetelnya ulang).

    Args:
        role_prefix: awalan untuk nama role **login** dan role pemilik objek.
            Kosong di produksi, dan ``guard db bootstrap-roles`` tidak punya
            flag untuk mengisinya. Ia ada untuk harness tes, supaya satu run
            tes tidak bisa menyentuh role run lain maupun role operator di
            cluster yang sama; lihat
            :func:`payment_guard.privileges.require_role_prefix`. Nama role
            **grup** tidak ikut ber-awalan, karena ``0002_roles.sql``
            menuliskannya harfiah.

    Raises:
        MigrationError: bila ``admin_url`` menunjuk host non-loopback dan
            ``allow_remote_admin`` tidak disetel, atau bila role penyambung
            tidak punya CREATEROLE.
        RolePrefixError: bila ``role_prefix`` bentuknya tidak sah.
    """
    require_loopback_admin(admin_url, allow_remote=allow_remote_admin)
    require_role_prefix(role_prefix)

    result = BootstrapResult(env_path=env_path, migrator_env_path=migrator_env_path)
    runtime_env = read_env_file(env_path)
    migrator_env = read_env_file(migrator_env_path)
    runtime_updates: dict[str, str] = {}
    migrator_updates: dict[str, str] = {}

    with connect(admin_url) as conn, conn.transaction(), conn.cursor() as cur:
        _require_createrole(cur)

        # 1. Role grup NOLOGIN.
        for process in PROCESSES:
            group = GROUP_ROLES[process]
            existed = _role_exists(cur, group)
            _create_group_role(cur, group)
            (result.group_roles_existing if existed else result.group_roles_created).append(group)

        # 2. Role pemilik objek (migrator). Bukan superuser, tanpa CREATEROLE.
        #    URL-nya hidup di file terpisah, tidak pernah di .env runtime.
        owner = owner_role(role_prefix)
        owner_existed = _role_exists(cur, owner)
        if owner_existed and migrator_env.get(MIGRATOR_ENV_KEY):
            result.login_roles_reused.append(owner)
        else:
            owner_password = _new_password()
            if owner_existed:
                _set_password(cur, owner, owner_password)
                result.login_roles_password_reset.append(owner)
            else:
                _create_login_role(cur, owner, owner_password)
                result.login_roles_created.append(owner)
            migrator_updates[MIGRATOR_ENV_KEY] = build_url(admin_url, owner, owner_password)

        # 3. Hak tingkat schema. Wewenang admin, bukan migrator.
        _schema_privileges(cur, role_prefix)

        # 4. Role LOGIN per proses, anggota role grup masing-masing.
        for process in PROCESSES:
            login = login_role(process, role_prefix)
            group = GROUP_ROLES[process]
            env_key = ENV_KEY_FOR_PROCESS[process]
            existed = _role_exists(cur, login)
            has_url = bool(runtime_env.get(env_key))

            if existed and has_url:
                result.login_roles_reused.append(login)
            else:
                password = _new_password()
                if existed:
                    _set_password(cur, login, password)
                    result.login_roles_password_reset.append(login)
                else:
                    _create_login_role(cur, login, password)
                    result.login_roles_created.append(login)
                runtime_updates[env_key] = build_url(admin_url, login, password)
            _grant_membership(cur, login, group)

    result.env_keys_written = write_env_file(env_path, runtime_updates)
    result.migrator_env_keys_written = write_env_file(migrator_env_path, migrator_updates)
    return result
