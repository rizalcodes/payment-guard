"""``guard db bootstrap-roles`` terhadap Postgres 16 sungguhan.

Menutup ST-41 (migrator tanpa CREATEROLE ditolak) dan ST-42 (role grup NOLOGIN
tidak bisa login; bootstrap idempoten), ditambah pemeriksaan bahwa rahasia
tidak bocor ke terminal.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from conftest import RoleNames
from payment_guard.bootstrap import (
    ENV_KEY_FOR_PROCESS,
    MIGRATOR_ENV_KEY,
    build_url,
    run_bootstrap_roles,
)
from payment_guard.config import ADMIN_URL_ENV_VAR
from payment_guard.config import read_env_file as read_env
from payment_guard.db import MigrationError
from payment_guard.privileges import (
    GROUP_ROLES,
    LOGIN_ROLES,
    OWNER_ROLE,
    PROCESSES,
    SCHEMA_USAGE,
)

pytestmark = pytest.mark.integration

#: Pola nama role terkelola. Dipakai sebagai GANTI daftar nama statis:
#: nama role login dan pemilik objek ber-awalan acak per tes (lihat
#: `RoleNames` di conftest.py), jadi daftar statis akan selalu kosong.
#:
#: Polanya cukup sempit untuk tidak ikut menangkap `guard_admin` maupun
#: role uji sementara seperti `peran_uji_lemah_*`. Role bawaan Postgres
#: disaring terpisah di kueri: `pg_database_owner` juga berakhiran `_owner`.
MANAGED_ROLE_REGEX = "(_rw|_app|_owner)$"


def role_attributes(url: str) -> dict[str, tuple[bool, ...]]:
    """Atribut semua role terkelola di cluster tes, apa pun awalannya."""
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT rolname, rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, "
            "rolinherit, rolreplication "
            "FROM pg_roles WHERE rolname ~ %s AND rolname NOT LIKE 'pg\\_%%' "
            "ORDER BY rolname",
            (MANAGED_ROLE_REGEX,),
        )
        return {row[0]: tuple(row[1:]) for row in cur.fetchall()}


def memberships(url: str) -> set[tuple[str, str]]:
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT member.rolname, grp.rolname "
            "FROM pg_auth_members m "
            "JOIN pg_roles member ON member.oid = m.member "
            "JOIN pg_roles grp ON grp.oid = m.roleid "
            "WHERE grp.rolname = ANY(%s)",
            (list(GROUP_ROLES.values()),),
        )
        return {(row[0], row[1]) for row in cur.fetchall()}


def schema_acl(url: str) -> str | None:
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT nspacl::text FROM pg_namespace WHERE nspname = 'public'")
        row = cur.fetchone()
        return row[0] if row else None


class TestRolesCreated:
    def test_group_roles_are_nologin(self, bootstrapped: dict[str, str], pristine_db: str) -> None:
        attrs = role_attributes(pristine_db)
        for process in PROCESSES:
            name = GROUP_ROLES[process]
            assert name in attrs, f"{name} tidak dibuat"
            can_login, is_super, can_createrole, can_createdb, _, can_replicate = attrs[name]
            assert can_login is False, f"{name} seharusnya NOLOGIN"
            assert is_super is False
            assert can_createrole is False
            assert can_createdb is False
            assert can_replicate is False

    def test_login_roles_exist_and_are_not_privileged(
        self, bootstrapped: dict[str, str], pristine_db: str, roles: RoleNames
    ) -> None:
        attrs = role_attributes(pristine_db)
        for process in PROCESSES:
            name = roles.login(process)
            can_login, is_super, can_createrole, can_createdb, inherits, _ = attrs[name]
            assert can_login is True
            assert is_super is False
            assert can_createrole is False
            assert can_createdb is False
            assert inherits is True, f"{name} harus INHERIT agar mewarisi hak role grup"

    def test_migrator_owner_is_not_superuser_and_cannot_create_roles(
        self, bootstrapped: dict[str, str], pristine_db: str, roles: RoleNames
    ) -> None:
        """Arsitektur v1.1 3.1: pemilik objek bukan superuser."""
        can_login, is_super, can_createrole, _, _, _ = role_attributes(pristine_db)[roles.owner]
        assert can_login is True
        assert is_super is False
        assert can_createrole is False

    def test_each_login_role_is_member_of_its_group(
        self, bootstrapped: dict[str, str], pristine_db: str, roles: RoleNames
    ) -> None:
        found = memberships(pristine_db)
        expected = {(roles.login(p), roles.group(p)) for p in PROCESSES}
        assert expected <= found

    def test_no_cross_membership(
        self, bootstrapped: dict[str, str], pristine_db: str, roles: RoleNames
    ) -> None:
        """guard_app tidak boleh jadi anggota executor_rw, dan seterusnya."""
        expected = {(roles.login(p), roles.group(p)) for p in PROCESSES}
        assert memberships(pristine_db) == expected


class TestLoginBehaviour:
    @pytest.mark.parametrize("process", PROCESSES)
    def test_group_role_cannot_login(
        self, bootstrapped: dict[str, str], pristine_db: str, process: str
    ) -> None:
        """ST-42: role grup NOLOGIN tidak bisa dipakai menyambung.

        Role grup sengaja diberi password yang diketahui lebih dulu. Tanpa itu,
        koneksi akan gagal pada tahap autentikasi dan tes ini hanya akan
        membuktikan "password salah" — bukan bahwa NOLOGIN-lah yang menahan.
        """
        group = GROUP_ROLES[process]
        known = "password-uji-yang-benar"
        with psycopg.connect(pristine_db, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(group), sql.Literal(known)
                )
            )

        with pytest.raises(psycopg.OperationalError) as exc:
            psycopg.connect(build_url(pristine_db, group, known), connect_timeout=5)
        assert "not permitted to log in" in str(exc.value)

        # Dan atributnya memang masih NOLOGIN.
        assert role_attributes(pristine_db)[group][0] is False

    @pytest.mark.parametrize("process", PROCESSES)
    def test_process_url_connects_as_the_right_role(
        self, bootstrapped: dict[str, str], process: str, roles: RoleNames
    ) -> None:
        url = bootstrapped[ENV_KEY_FOR_PROCESS[process]]
        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute("SELECT current_user")
            row = cur.fetchone()
        assert row is not None
        assert row[0] == roles.login(process)

    def test_migrator_url_connects_as_owner(
        self, bootstrapped: dict[str, str], roles: RoleNames
    ) -> None:
        with psycopg.connect(bootstrapped[MIGRATOR_ENV_KEY]) as conn, conn.cursor() as cur:
            cur.execute("SELECT current_user")
            row = cur.fetchone()
        assert row is not None
        assert row[0] == roles.owner

    def test_wrong_password_is_rejected(
        self, bootstrapped: dict[str, str], pristine_db: str, roles: RoleNames
    ) -> None:
        """Password acak benar-benar dipasang, bukan kosong."""
        url = build_url(pristine_db, roles.login("guard"), "password-yang-salah")
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(url, connect_timeout=5)


class TestSchemaPrivileges:
    def test_public_loses_all_schema_privileges(
        self, bootstrapped: dict[str, str], pristine_db: str
    ) -> None:
        """ST-40: tidak ada hak untuk PUBLIC di schema aplikasi."""
        with psycopg.connect(pristine_db) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_namespace n, "
                "aclexplode(n.nspacl) a WHERE n.nspname = 'public' AND a.grantee = 0"
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == 0, "masih ada hak yang diberikan ke PUBLIC pada schema public"

    @pytest.mark.parametrize("process", SCHEMA_USAGE)
    def test_group_role_has_schema_usage(
        self, bootstrapped: dict[str, str], pristine_db: str, process: str
    ) -> None:
        """ST-41: keadaan efektif diperiksa dengan has_schema_privilege."""
        with psycopg.connect(pristine_db) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT has_schema_privilege(%s, 'public', %s)", (GROUP_ROLES[process], "USAGE")
            )
            usage = cur.fetchone()
            cur.execute(
                "SELECT has_schema_privilege(%s, 'public', %s)", (GROUP_ROLES[process], "CREATE")
            )
            create = cur.fetchone()
        assert usage is not None and usage[0] is True
        assert create is not None and create[0] is False, (
            "role aplikasi tidak boleh CREATE di schema"
        )

    def test_migrator_has_create_on_schema(
        self, bootstrapped: dict[str, str], pristine_db: str, roles: RoleNames
    ) -> None:
        with psycopg.connect(pristine_db) as conn, conn.cursor() as cur:
            cur.execute("SELECT has_schema_privilege(%s, 'public', 'CREATE')", (roles.owner,))
            row = cur.fetchone()
        assert row is not None and row[0] is True


class TestIdempotence:
    """ST-42: dijalankan dua kali, hasilnya sama."""

    def test_second_run_leaves_roles_identical(
        self, pristine_db: str, env_file: Path, migrator_env_file: Path
    ) -> None:
        run_bootstrap_roles(pristine_db, env_file, migrator_env_file)
        before = (role_attributes(pristine_db), memberships(pristine_db), schema_acl(pristine_db))

        run_bootstrap_roles(pristine_db, env_file, migrator_env_file)
        after = (role_attributes(pristine_db), memberships(pristine_db), schema_acl(pristine_db))

        assert after == before

    def test_second_run_rewrites_neither_file(
        self, pristine_db: str, env_file: Path, migrator_env_file: Path
    ) -> None:
        """Password tidak dirotasi diam-diam: proses yang jalan tidak putus."""
        run_bootstrap_roles(pristine_db, env_file, migrator_env_file)
        first_runtime = env_file.read_bytes()
        first_migrator = migrator_env_file.read_bytes()

        result = run_bootstrap_roles(pristine_db, env_file, migrator_env_file)
        assert env_file.read_bytes() == first_runtime
        assert migrator_env_file.read_bytes() == first_migrator
        assert result.env_keys_written == []
        assert result.migrator_env_keys_written == []
        assert sorted(result.login_roles_reused) == sorted([*LOGIN_ROLES.values(), OWNER_ROLE])

    def test_second_run_reports_roles_as_existing(
        self, pristine_db: str, env_file: Path, migrator_env_file: Path
    ) -> None:
        first = run_bootstrap_roles(pristine_db, env_file, migrator_env_file)
        assert sorted(first.group_roles_created) == sorted(GROUP_ROLES.values())

        second = run_bootstrap_roles(pristine_db, env_file, migrator_env_file)
        assert second.group_roles_created == []
        assert sorted(second.group_roles_existing) == sorted(GROUP_ROLES.values())

    def test_credentials_still_work_after_second_run(
        self, pristine_db: str, env_file: Path, migrator_env_file: Path
    ) -> None:
        run_bootstrap_roles(pristine_db, env_file, migrator_env_file)
        run_bootstrap_roles(pristine_db, env_file, migrator_env_file)

        env = read_env(env_file)
        for process in PROCESSES:
            with psycopg.connect(env[ENV_KEY_FOR_PROCESS[process]]) as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                assert cur.fetchone() == (1,)

        migrator_url = read_env(migrator_env_file)[MIGRATOR_ENV_KEY]
        with psycopg.connect(migrator_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT current_user")
            assert cur.fetchone() == (OWNER_ROLE,)

    def test_runtime_password_is_restored_when_env_entry_is_lost(
        self, pristine_db: str, env_file: Path, migrator_env_file: Path
    ) -> None:
        """Password tidak bisa dibaca kembali dari Postgres.

        Kalau barisnya hilang dari .env, satu-satunya pemulihan adalah menyetel
        password baru. Bootstrap melakukannya dan melaporkannya.
        """
        run_bootstrap_roles(pristine_db, env_file, migrator_env_file)
        env = read_env(env_file)
        del env["GUARD_DB_URL"]
        env_file.write_text(
            "\n".join(f"{k}={v}" for k, v in env.items()) + "\n", encoding="utf-8", newline="\n"
        )

        result = run_bootstrap_roles(pristine_db, env_file, migrator_env_file)
        assert result.login_roles_password_reset == [LOGIN_ROLES["guard"]]
        assert result.env_keys_written == ["GUARD_DB_URL"]
        # File migrator tidak ikut tersentuh.
        assert result.migrator_env_keys_written == []

        with psycopg.connect(read_env(env_file)["GUARD_DB_URL"]) as conn, conn.cursor() as cur:
            cur.execute("SELECT current_user")
            assert cur.fetchone() == (LOGIN_ROLES["guard"],)

    def test_migrator_password_is_restored_when_its_file_is_lost(
        self, pristine_db: str, env_file: Path, migrator_env_file: Path
    ) -> None:
        """Kehilangan .env.migrator tidak merotasi password runtime."""
        run_bootstrap_roles(pristine_db, env_file, migrator_env_file)
        runtime_before = env_file.read_bytes()
        migrator_env_file.unlink()

        result = run_bootstrap_roles(pristine_db, env_file, migrator_env_file)
        assert result.login_roles_password_reset == [OWNER_ROLE]
        assert result.migrator_env_keys_written == [MIGRATOR_ENV_KEY]
        assert result.env_keys_written == []
        assert env_file.read_bytes() == runtime_before

        with psycopg.connect(read_env(migrator_env_file)[MIGRATOR_ENV_KEY]) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_user")
                assert cur.fetchone() == (OWNER_ROLE,)


class TestEnvFileSplit:
    """M0.1: URL migrator tidak boleh mendarat di ``.env``."""

    def test_migrator_url_goes_only_to_the_migrator_file(
        self, pristine_db: str, env_file: Path, migrator_env_file: Path
    ) -> None:
        run_bootstrap_roles(pristine_db, env_file, migrator_env_file)

        runtime = read_env(env_file)
        migrator = read_env(migrator_env_file)

        assert MIGRATOR_ENV_KEY not in runtime
        assert MIGRATOR_ENV_KEY in migrator
        assert set(runtime) == set(ENV_KEY_FOR_PROCESS.values())
        assert set(migrator) == {MIGRATOR_ENV_KEY}

    def test_owner_role_name_never_appears_in_the_runtime_file(
        self, pristine_db: str, env_file: Path, migrator_env_file: Path
    ) -> None:
        """Bukan hanya nama variabelnya: URL-nya sendiri tidak ada di sana."""
        run_bootstrap_roles(pristine_db, env_file, migrator_env_file)
        runtime_text = env_file.read_text(encoding="utf-8")
        assert OWNER_ROLE not in runtime_text

        migrator_url = read_env(migrator_env_file)[MIGRATOR_ENV_KEY]
        assert migrator_url not in runtime_text
        migrator_password = migrator_url.split(":")[2].split("@")[0]
        assert migrator_password not in runtime_text

    def test_runtime_passwords_never_appear_in_the_migrator_file(
        self, pristine_db: str, env_file: Path, migrator_env_file: Path
    ) -> None:
        run_bootstrap_roles(pristine_db, env_file, migrator_env_file)
        migrator_text = migrator_env_file.read_text(encoding="utf-8")
        for url in read_env(env_file).values():
            assert url not in migrator_text
            assert url.split(":")[2].split("@")[0] not in migrator_text

    def test_both_files_are_created_even_in_a_fresh_directory(
        self, pristine_db: str, tmp_path: Path
    ) -> None:
        nested = tmp_path / "belum" / "ada"
        env_file = nested / ".env"
        migrator_env_file = nested / ".env.migrator"
        run_bootstrap_roles(pristine_db, env_file, migrator_env_file)
        assert env_file.is_file()
        assert migrator_env_file.is_file()


class TestCreateRoleRequirement:
    """ST-41: bootstrap menolak URL yang tidak punya CREATEROLE."""

    #: Password role uji. Hanya hidup selama satu tes, lalu role-nya dihapus.
    WEAK_PASSWORD = "rahasia-uji"

    @pytest.fixture
    def weak(self, pristine_db: str) -> Iterator[tuple[str, str]]:
        """Role LOGIN tanpa CREATEROLE. Mengembalikan (url, nama role).

        Namanya diacak dan role-nya dihapus di teardown: role Postgres bersifat
        cluster-wide, jadi sisa dari satu tes akan merusak tes berikutnya.
        """
        name = f"peran_uji_lemah_{secrets.token_hex(4)}"
        with psycopg.connect(pristine_db, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                sql.SQL("CREATE ROLE {} LOGIN NOCREATEROLE NOSUPERUSER PASSWORD {}").format(
                    sql.Identifier(name), sql.Literal(self.WEAK_PASSWORD)
                )
            )
        try:
            yield build_url(pristine_db, name, self.WEAK_PASSWORD), name
        finally:
            with psycopg.connect(pristine_db, autocommit=True) as conn, conn.cursor() as cur:
                cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))

    def test_rejected_with_clear_message(
        self, weak: tuple[str, str], env_file: Path, migrator_env_file: Path
    ) -> None:
        url, name = weak
        with pytest.raises(MigrationError) as exc:
            run_bootstrap_roles(url, env_file, migrator_env_file)
        message = str(exc.value)
        assert "CREATEROLE" in message
        assert name in message

    def test_nothing_is_created_on_rejection(
        self,
        weak: tuple[str, str],
        env_file: Path,
        migrator_env_file: Path,
        pristine_db: str,
    ) -> None:
        url, _ = weak
        with pytest.raises(MigrationError):
            run_bootstrap_roles(url, env_file, migrator_env_file)
        assert role_attributes(pristine_db) == {}
        assert not env_file.exists()
        assert not migrator_env_file.exists()

    def test_message_does_not_leak_the_url(
        self, weak: tuple[str, str], env_file: Path, migrator_env_file: Path
    ) -> None:
        url, _ = weak
        with pytest.raises(MigrationError) as exc:
            run_bootstrap_roles(url, env_file, migrator_env_file)
        assert self.WEAK_PASSWORD not in str(exc.value)
        assert "postgresql://" not in str(exc.value)


class TestSecretsDoNotLeak:
    def test_summary_contains_no_passwords(
        self,
        pristine_db: str,
        env_file: Path,
        migrator_env_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = run_bootstrap_roles(pristine_db, env_file, migrator_env_file)
        printed = "\n".join(result.summary_lines())

        all_urls = {**read_env(env_file), **read_env(migrator_env_file)}
        for url in all_urls.values():
            password = url.split(":")[2].split("@")[0]
            assert password not in printed, "password muncul di ringkasan"
            assert url not in printed
        assert capsys.readouterr().out == "", "bootstrap mencetak sesuatu di luar ringkasan"

    def test_admin_url_is_not_written_to_either_file(
        self, pristine_db: str, env_file: Path, migrator_env_file: Path
    ) -> None:
        """Security v1.1 I6."""
        run_bootstrap_roles(pristine_db, env_file, migrator_env_file)
        for path in (env_file, migrator_env_file):
            text = path.read_text(encoding="utf-8")
            assert ADMIN_URL_ENV_VAR not in text
            assert "guard_admin" not in text
            assert "devonly_not_a_secret" not in text

    def test_runtime_file_has_exactly_the_five_runtime_keys(
        self, pristine_db: str, env_file: Path, migrator_env_file: Path
    ) -> None:
        run_bootstrap_roles(pristine_db, env_file, migrator_env_file)
        assert set(read_env(env_file)) == set(ENV_KEY_FOR_PROCESS.values())
        assert len(read_env(env_file)) == 5

    def test_passwords_differ_per_role(
        self, pristine_db: str, env_file: Path, migrator_env_file: Path
    ) -> None:
        run_bootstrap_roles(pristine_db, env_file, migrator_env_file)
        all_urls = {**read_env(env_file), **read_env(migrator_env_file)}
        passwords = {url.split(":")[2].split("@")[0] for url in all_urls.values()}
        assert len(passwords) == len(ENV_KEY_FOR_PROCESS) + 1
        for password in passwords:
            assert len(password) >= 32


class TestRemoteAdminRefusal:
    """M0.1: URL admin non-loopback ditolak, kecuali flag eksplisit.

    Jalur penolakan tanpa database sudah diuji di unit; di sini yang diuji
    adalah sisi sebaliknya, bahwa flag benar-benar membuka jalan sampai
    bootstrap selesai terhadap Postgres sungguhan.
    """

    def test_loopback_url_is_accepted(
        self, pristine_db: str, env_file: Path, migrator_env_file: Path
    ) -> None:
        assert "127.0.0.1" in pristine_db
        result = run_bootstrap_roles(pristine_db, env_file, migrator_env_file)
        assert sorted(result.group_roles_created) == sorted(GROUP_ROLES.values())

    def test_allow_remote_admin_also_works_for_a_loopback_url(
        self, pristine_db: str, env_file: Path, migrator_env_file: Path
    ) -> None:
        """Flag melonggarkan pemeriksaan, bukan mengubah perilaku lainnya."""
        result = run_bootstrap_roles(
            pristine_db, env_file, migrator_env_file, allow_remote_admin=True
        )
        assert sorted(result.group_roles_created) == sorted(GROUP_ROLES.values())
        assert set(read_env(env_file)) == set(ENV_KEY_FOR_PROCESS.values())

    def test_non_loopback_url_is_refused_without_the_flag(
        self, env_file: Path, migrator_env_file: Path
    ) -> None:
        remote = "postgresql://admin:pw@10.1.2.3:5434/payment_guard"
        with pytest.raises(MigrationError, match="bukan loopback"):
            run_bootstrap_roles(remote, env_file, migrator_env_file)
        assert not env_file.exists()
        assert not migrator_env_file.exists()
