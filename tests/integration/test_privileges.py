"""Matriks hak minimum, diperiksa terhadap database yang sudah dimigrasi.

Menutup ST-33, ST-34, ST-38, ST-39, ST-40, dan ST-43.

Pemeriksaan dilakukan dua arah dan pada dua lapis:

* **Dua arah:** objek di ``pg_class`` tanpa baris matriks menggagalkan tes, dan
  baris matriks tanpa objek di database juga menggagalkan tes.
* **Dua lapis:** hak diperiksa pada role grup (penerima GRANT) dan pada role
  LOGIN (yang mewarisinya). Lapis kedua yang menentukan: itulah yang dipakai
  proses sungguhan.

Setiap hak yang tidak ada di matriks harus bernilai false. Tes ini karena itu
gagal baik untuk hak yang kurang maupun untuk hak yang berlebih.

Catatan bentuk: kombinasi (tabel x role x hak) diperiksa dengan perulangan di
dalam satu tes, bukan dengan ``parametrize``. Setiap tes di modul ini
membangun database baru lewat fixture ``migrated``; memparametrisasi ratusan
kombinasi akan membangun ulang database ratusan kali. Supaya kegagalan tetap
mudah dibaca, setiap perulangan mengumpulkan SEMUA pelanggaran lalu
melaporkannya sekaligus, bukan berhenti di yang pertama.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from conftest import RoleNames
from payment_guard.bootstrap import ENV_KEY_FOR_PROCESS, MIGRATOR_ENV_KEY
from payment_guard.privileges import (
    FORBIDDEN_TABLE_PRIVILEGES,
    GROUP_ROLES,
    OWNER_ONLY_TABLES,
    PROCESSES,
    SEQUENCE_MATRIX,
    TABLE_MATRIX,
    all_matrix_sequences,
    all_matrix_tables,
    grant_for,
)

pytestmark = pytest.mark.integration

#: Seluruh hak tingkat tabel yang dikenal Postgres 16.
ALL_TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", *FORBIDDEN_TABLE_PRIVILEGES)

#: Seluruh hak tingkat sequence. Matriks hanya memberi USAGE.
ALL_SEQUENCE_PRIVILEGES = ("USAGE", "SELECT", "UPDATE")


def all_app_roles(roles: RoleNames) -> list[str]:
    """Role aplikasi: role grup dan role LOGIN sekaligus.

    Fungsi, bukan konstanta: nama role login ber-awalan acak per tes, jadi
    daftar tingkat modul akan menyebut role yang tidak pernah ada.
    """
    return [*roles.all_groups(), *roles.all_logins()]


def _query(url: str, statement: str, params: tuple[object, ...] = ()) -> list[tuple]:
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(statement, params)
        return cur.fetchall()


def columns_of(cur: psycopg.Cursor, table: str) -> list[str]:
    cur.execute(
        "SELECT attname FROM pg_attribute "
        "WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped "
        "ORDER BY attnum",
        (table,),
    )
    return [row[0] for row in cur.fetchall()]


@pytest.fixture
def owner_url(migrated: dict[str, str]) -> str:
    return migrated[MIGRATOR_ENV_KEY]


@pytest.fixture
def owner_cur(owner_url: str) -> Iterator[psycopg.Cursor]:
    """Satu kursor pemilik untuk semua kueri katalog dalam satu tes."""
    with psycopg.connect(owner_url) as conn, conn.cursor() as cur:
        yield cur


@pytest.fixture
def process_conn(migrated: dict[str, str]):
    """Pabrik koneksi sebagai role LOGIN sebuah proses."""

    def factory(process: str) -> psycopg.Connection:
        return psycopg.connect(migrated[ENV_KEY_FOR_PROCESS[process]])

    return factory


def has_table(cur: psycopg.Cursor, role: str, table: str, privilege: str) -> bool:
    cur.execute("SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege))
    row = cur.fetchone()
    assert row is not None
    return bool(row[0])


def has_column(cur: psycopg.Cursor, role: str, table: str, column: str, privilege: str) -> bool:
    cur.execute("SELECT has_column_privilege(%s, %s, %s, %s)", (role, table, column, privilege))
    row = cur.fetchone()
    assert row is not None
    return bool(row[0])


def has_sequence(cur: psycopg.Cursor, role: str, sequence: str, privilege: str) -> bool:
    cur.execute("SELECT has_sequence_privilege(%s, %s, %s)", (role, sequence, privilege))
    row = cur.fetchone()
    assert row is not None
    return bool(row[0])


# --------------------------------------------------------------------------- #
# ST-38: matriks dicek dua arah dengan pg_class
# --------------------------------------------------------------------------- #


class TestObjectInventory:
    def test_tables_in_database_match_the_matrix(self, owner_url: str) -> None:
        found = {
            row[0]
            for row in _query(
                owner_url,
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')",
            )
        }
        expected = all_matrix_tables()
        assert found - expected == set(), f"tabel tanpa baris matriks: {sorted(found - expected)}"
        assert expected - found == set(), f"baris matriks tanpa tabel: {sorted(expected - found)}"

    def test_sequences_in_database_match_the_matrix(self, owner_url: str) -> None:
        found = {
            row[0]
            for row in _query(
                owner_url,
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'S'",
            )
        }
        expected = all_matrix_sequences()
        assert found - expected == set(), (
            f"sequence tanpa baris matriks: {sorted(found - expected)}"
        )
        assert expected - found == set(), (
            f"baris matriks tanpa sequence: {sorted(expected - found)}"
        )

    def test_every_matrix_column_exists(self, owner_cur: psycopg.Cursor) -> None:
        """Nama kolom di matriks harus benar-benar ada.

        Matriks di Python dipakai untuk membentuk ekspektasi tes lain: kalau
        nama kolomnya salah ketik, ekspektasinya ikut salah dan tes jadi tidak
        berarti. Jadi nama kolom diverifikasi terpisah.
        """
        problems = []
        for table, row in TABLE_MATRIX.items():
            actual = set(columns_of(owner_cur, table))
            for process, grant in row.items():
                for column in grant.update_columns or ():
                    if column not in actual:
                        problems.append(f"{table}.{column} (matriks {process})")
        assert problems == [], problems

    def test_all_objects_are_owned_by_the_migrator(self, owner_url: str, roles: RoleNames) -> None:
        """Arsitektur v1.1 3.1: pemilik objek = role migrator, bukan superuser."""
        rows = _query(
            owner_url,
            "SELECT c.relname, pg_get_userbyid(c.relowner) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'S')",
        )
        wrong = [(name, owner) for name, owner in rows if owner != roles.owner]
        assert wrong == [], wrong


# --------------------------------------------------------------------------- #
# ST-39: hak efektif tiap (role, objek, hak) sesuai matriks, tanpa hak tambahan
# --------------------------------------------------------------------------- #


class TestEffectiveTablePrivileges:
    def test_group_roles_match_matrix_exactly(
        self, owner_cur: psycopg.Cursor, roles: RoleNames
    ) -> None:
        """Role grup adalah penerima GRANT langsung di 0002_roles.sql."""
        problems = []
        for table in sorted(TABLE_MATRIX):
            for process in PROCESSES:
                allowed = grant_for(table, process).privileges()
                for privilege in ALL_TABLE_PRIVILEGES:
                    expected = privilege in allowed
                    actual = has_table(owner_cur, roles.group(process), table, privilege)
                    if actual is not expected:
                        problems.append(
                            f"{roles.group(process)}.{table}.{privilege}: "
                            f"ada={actual}, harus={expected}"
                        )
        assert problems == [], problems

    def test_login_roles_inherit_exactly_the_same(
        self, owner_cur: psycopg.Cursor, roles: RoleNames
    ) -> None:
        """Lapis yang sebenarnya dipakai proses: role LOGIN lewat keanggotaan grup."""
        problems = []
        for table in sorted(TABLE_MATRIX):
            for process in PROCESSES:
                allowed = grant_for(table, process).privileges()
                for privilege in ALL_TABLE_PRIVILEGES:
                    expected = privilege in allowed
                    actual = has_table(owner_cur, roles.login(process), table, privilege)
                    if actual is not expected:
                        problems.append(
                            f"{roles.login(process)}.{table}.{privilege}: "
                            f"ada={actual}, harus={expected}"
                        )
        assert problems == [], problems

    def test_no_role_has_forbidden_privileges(
        self, owner_cur: psycopg.Cursor, roles: RoleNames
    ) -> None:
        """ST-34: tidak ada DELETE, TRUNCATE, REFERENCES, atau TRIGGER di mana pun."""
        problems = []
        for table in sorted(all_matrix_tables()):
            for role in all_app_roles(roles):
                for privilege in FORBIDDEN_TABLE_PRIVILEGES:
                    if has_table(owner_cur, role, table, privilege):
                        problems.append(f"{role} punya {privilege} pada {table}")
        assert problems == [], problems


class TestEffectiveColumnPrivileges:
    """ST-39 di tingkat kolom, memakai has_column_privilege."""

    def test_update_columns_match_matrix(self, owner_cur: psycopg.Cursor, roles: RoleNames) -> None:
        problems = []
        for table in sorted(TABLE_MATRIX):
            columns = columns_of(owner_cur, table)
            for process in PROCESSES:
                grant = grant_for(table, process)
                role = roles.login(process)
                allowed = set(grant.update_columns or ())
                for column in columns:
                    if grant.table_level_update:
                        expected = True
                    elif grant.update_columns is None:
                        expected = False
                    else:
                        expected = column in allowed
                    actual = has_column(owner_cur, role, table, column, "UPDATE")
                    if actual is not expected:
                        problems.append(
                            f"{role}: {table}.{column} UPDATE ada={actual}, harus={expected}"
                        )
        assert problems == [], problems

    def test_select_is_never_granted_per_column(
        self, owner_cur: psycopg.Cursor, roles: RoleNames
    ) -> None:
        """Matriks memberi SELECT di tingkat tabel saja.

        Role tanpa SELECT pada tabel tidak boleh punya SELECT pada kolom mana
        pun: itu akan menjadi hak tambahan yang tidak tercatat di matriks.
        """
        problems = []
        for table in sorted(TABLE_MATRIX):
            columns = columns_of(owner_cur, table)
            for process in PROCESSES:
                expected = grant_for(table, process).select
                role = roles.login(process)
                for column in columns:
                    actual = has_column(owner_cur, role, table, column, "SELECT")
                    if actual is not expected:
                        problems.append(f"{role}: {table}.{column} SELECT ada={actual}")
        assert problems == [], problems

    def test_insert_is_never_granted_per_column(
        self, owner_cur: psycopg.Cursor, roles: RoleNames
    ) -> None:
        problems = []
        for table in sorted(TABLE_MATRIX):
            columns = columns_of(owner_cur, table)
            for process in PROCESSES:
                expected = grant_for(table, process).insert
                role = roles.login(process)
                for column in columns:
                    actual = has_column(owner_cur, role, table, column, "INSERT")
                    if actual is not expected:
                        problems.append(f"{role}: {table}.{column} INSERT ada={actual}")
        assert problems == [], problems


class TestEffectiveSequencePrivileges:
    def test_sequence_privileges_match_matrix(
        self, owner_cur: psycopg.Cursor, roles: RoleNames
    ) -> None:
        """Hanya USAGE, hanya untuk proses yang terdaftar."""
        problems = []
        for sequence in sorted(SEQUENCE_MATRIX):
            owners = SEQUENCE_MATRIX[sequence]
            for process in PROCESSES:
                for privilege in ALL_SEQUENCE_PRIVILEGES:
                    expected = privilege == "USAGE" and process in owners
                    for role in (roles.group(process), roles.login(process)):
                        actual = has_sequence(owner_cur, role, sequence, privilege)
                        if actual is not expected:
                            problems.append(
                                f"{role}.{sequence}.{privilege}: ada={actual}, harus={expected}"
                            )
        assert problems == [], problems


# --------------------------------------------------------------------------- #
# ST-40: tidak ada grant ke PUBLIC; schema_migrations hanya pemilik
# --------------------------------------------------------------------------- #


class TestNoPublicGrants:
    def test_no_table_or_sequence_acl_mentions_public(self, owner_url: str) -> None:
        rows = _query(
            owner_url,
            "SELECT c.relname, a.privilege_type FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace, "
            "aclexplode(c.relacl) a "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'S') AND a.grantee = 0",
        )
        assert rows == [], f"ada hak untuk PUBLIC: {rows}"

    def test_no_column_acl_mentions_public(self, owner_url: str) -> None:
        rows = _query(
            owner_url,
            "SELECT c.relname, att.attname, a.privilege_type FROM pg_attribute att "
            "JOIN pg_class c ON c.oid = att.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace, "
            "aclexplode(att.attacl) a "
            "WHERE n.nspname = 'public' AND a.grantee = 0",
        )
        assert rows == [], f"ada hak kolom untuk PUBLIC: {rows}"

    def test_no_grant_option_was_handed_out(self, owner_url: str, roles: RoleNames) -> None:
        """Role aplikasi tidak boleh bisa meneruskan haknya ke role lain."""
        rows = _query(
            owner_url,
            "SELECT c.relname, pg_get_userbyid(a.grantee), a.privilege_type "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, "
            "aclexplode(c.relacl) a "
            "WHERE n.nspname = 'public' AND a.is_grantable "
            "AND pg_get_userbyid(a.grantee) <> %s",
            (roles.owner,),
        )
        assert rows == [], f"ada hak dengan GRANT OPTION: {rows}"


class TestOwnerOnlyTables:
    def test_no_role_has_any_privilege(self, owner_cur: psycopg.Cursor, roles: RoleNames) -> None:
        problems = []
        for table in sorted(OWNER_ONLY_TABLES):
            for role in all_app_roles(roles):
                for privilege in ALL_TABLE_PRIVILEGES:
                    if has_table(owner_cur, role, table, privilege):
                        problems.append(f"{role} punya {privilege} pada {table}")
        assert problems == [], problems

    def test_select_is_refused_at_runtime(self, process_conn) -> None:
        for process in PROCESSES:
            with process_conn(process) as conn, conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("SELECT * FROM schema_migrations")
                conn.rollback()


# --------------------------------------------------------------------------- #
# ST-33 dan ST-34: perilaku nyata, bukan hanya katalog
# --------------------------------------------------------------------------- #


class TestCrossRoleRefusals:
    """ST-33: contoh konkret dari dokumen, diuji sebagai perilaku."""

    def test_executor_cannot_update_decisions(self, process_conn) -> None:
        with process_conn("executor") as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("UPDATE decisions SET status = 'approved' WHERE false")
            conn.rollback()

    def test_guard_cannot_insert_executions(self, process_conn) -> None:
        with process_conn("guard") as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "INSERT INTO executions "
                    "(decision_id, tx_hash, from_addr, to_addr, amount_base, nonce, "
                    "raw_tx, status) "
                    "VALUES (gen_random_uuid(), %s, %s, %s, 1, 0, %s, 'signed')",
                    ("0x" + "11" * 32, "0x" + "22" * 20, "0x" + "33" * 20, b"\x00"),
                )
            conn.rollback()

    def test_watcher_cannot_select_decisions(self, process_conn) -> None:
        with process_conn("watcher") as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT count(*) FROM decisions")
            conn.rollback()

    def test_guard_cannot_update_approval_columns(self, process_conn) -> None:
        """Guard hanya boleh kolom kedaluwarsa lazy, bukan kolom approval."""
        with process_conn("guard") as conn, conn.cursor() as cur:
            for statement in (
                "UPDATE decisions SET envelope = '{}'::jsonb WHERE false",
                "UPDATE decisions SET approved_at = now() WHERE false",
                "UPDATE decisions SET valid_until = now() WHERE false",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(statement)
                conn.rollback()

    def test_guard_can_update_its_own_columns(self, process_conn) -> None:
        """Sisi lain dari tes di atas: hak yang memang ada harus berfungsi."""
        with process_conn("guard") as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE decisions SET status = 'expired', "
                "reason_code = 'human_wait_timeout', decided_at = now() WHERE false"
            )
            conn.rollback()

    def test_approver_can_update_approval_columns(self, process_conn) -> None:
        with process_conn("approver") as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE decisions SET status = 'approved', reason_code = 'approved_by_human', "
                "decided_at = now(), approved_at = now(), valid_until = now(), "
                "envelope = '{}'::jsonb WHERE false"
            )
            conn.rollback()

    def test_approver_cannot_insert_decisions(self, process_conn) -> None:
        """Matriks memberi approver SELECT dan UPDATE saja pada decisions."""
        with process_conn("approver") as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "INSERT INTO decisions (id, agent_id, mandate_hash, chain_id, recipient, "
                    "amount_base, status, reason_code, decided_at) "
                    "VALUES (gen_random_uuid(), 'a', %s, 1, %s, 1, 'approved', 'approved', now())",
                    ("0" * 64, "0x" + "44" * 20),
                )
            conn.rollback()

    def test_reconciler_cannot_touch_watcher_state(self, process_conn) -> None:
        with process_conn("reconciler") as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT * FROM watcher_state")
            conn.rollback()


class TestNoDdlForApplicationRoles:
    """ST-34: role aplikasi tidak bisa DROP, ALTER, TRUNCATE, atau CREATE."""

    def test_ddl_is_refused_for_every_process(self, process_conn) -> None:
        statements = [
            "DROP TABLE audit_log",
            "ALTER TABLE audit_log ADD COLUMN disuntikkan int",
            "TRUNCATE audit_log",
            "CREATE TABLE tabel_penyusup (id int)",
            # RR-2 dibatasi: hanya pemilik yang bisa melepas trigger audit.
            "DROP TRIGGER audit_log_no_change ON audit_log",
            "ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_change",
        ]
        problems = []
        for process in PROCESSES:
            with process_conn(process) as conn, conn.cursor() as cur:
                for statement in statements:
                    try:
                        cur.execute(statement)
                    except psycopg.errors.InsufficientPrivilege:
                        pass
                    except psycopg.Error as exc:
                        problems.append(f"{process}: {statement} -> {type(exc).__name__}")
                    else:
                        problems.append(f"{process}: {statement} TIDAK ditolak")
                    conn.rollback()
        assert problems == [], problems


class TestAuditLogIsAppendOnly:
    """ST-34: semua role bisa INSERT; tidak ada yang bisa UPDATE atau DELETE."""

    def test_every_process_can_insert(self, process_conn) -> None:
        for process in PROCESSES:
            with process_conn(process) as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO audit_log (actor, event, payload) VALUES (%s, %s, %s)",
                    (process, "uji_hak_akses", '{"catatan": "tes"}'),
                )
                conn.commit()

    def test_no_process_can_update_or_delete(self, process_conn) -> None:
        problems = []
        for process in PROCESSES:
            with process_conn(process) as conn, conn.cursor() as cur:
                for statement in (
                    "UPDATE audit_log SET event = 'dipalsukan' WHERE false",
                    "DELETE FROM audit_log WHERE false",
                ):
                    try:
                        cur.execute(statement)
                    except psycopg.errors.InsufficientPrivilege:
                        pass
                    else:
                        problems.append(f"{process}: {statement} TIDAK ditolak")
                    conn.rollback()
        assert problems == [], problems

    def test_owner_is_still_blocked_by_the_trigger(self, owner_url: str) -> None:
        """ST-10: pemilik tidak dibatasi hak akses, tapi trigger tetap menolak.

        Ini lapis kedua, bukan jaminan: pemilik bisa melepas trigger (RR-2).
        """
        with psycopg.connect(owner_url) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO mandates (mandate_hash, mandate_id, agent_id, content) "
                "VALUES (%s, 'm1', 'agen-uji', '{}'::jsonb)",
                ("a" * 64,),
            )
            cur.execute(
                "INSERT INTO audit_log (actor, event, payload) VALUES ('migrator', 'uji', '{}')"
            )
            conn.commit()

            blocked_by_trigger = [
                "UPDATE mandates SET mandate_id = 'x'",
                "DELETE FROM mandates",
                "UPDATE audit_log SET event = 'x'",
                "DELETE FROM audit_log",
                "TRUNCATE audit_log",
            ]
            for statement in blocked_by_trigger:
                with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                    cur.execute(statement)
                conn.rollback()

            # TRUNCATE mandates tidak pernah sampai ke trigger: Postgres menolak
            # lebih dulu karena active_mandates punya foreign key ke sana.
            # Keduanya menutup jalan yang sama, tapi tesnya harus jujur soal
            # lapis mana yang sebenarnya bekerja.
            with pytest.raises(psycopg.errors.FeatureNotSupported, match="foreign key"):
                cur.execute("TRUNCATE mandates")
            conn.rollback()

            # Dengan CASCADE, FK bukan lagi penghalang, dan trigger yang menolak.
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                cur.execute("TRUNCATE mandates CASCADE")
            conn.rollback()

            # Barisnya memang masih ada.
            cur.execute("SELECT count(*) FROM mandates")
            assert cur.fetchone() == (1,)


# --------------------------------------------------------------------------- #
# ST-43: INSERT ke tabel BIGSERIAL butuh USAGE pada sequence
# --------------------------------------------------------------------------- #


class TestSequenceUsageIsRequired:
    """ST-43: tanpa USAGE pada sequence, INSERT ditolak; sesudah grant, berhasil.

    Hak dicabut lalu dipulihkan pada grant produksi yang sesungguhnya, bukan
    pada role tiruan, supaya yang diuji benar-benar baris matriks itu.
    """

    @staticmethod
    def _set_usage(owner_url: str, *, granted: bool) -> None:
        group = GROUP_ROLES["executor"]
        template = (
            "GRANT USAGE ON SEQUENCE audit_log_id_seq TO {}"
            if granted
            else "REVOKE USAGE ON SEQUENCE audit_log_id_seq FROM {}"
        )
        with psycopg.connect(owner_url, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(sql.SQL(template).format(sql.Identifier(group)))

    def test_insert_fails_without_usage_and_works_after_grant(
        self, owner_url: str, owner_cur: psycopg.Cursor, process_conn, roles: RoleNames
    ) -> None:
        role = roles.login("executor")
        insert = "INSERT INTO audit_log (actor, event, payload) VALUES ('executor', 'st43', '{}')"

        # 1. Keadaan produksi: INSERT berhasil.
        with process_conn("executor") as conn, conn.cursor() as cur:
            cur.execute(insert)
            conn.commit()

        # 2. Cabut USAGE pada sequence saja. INSERT pada tabel tetap diizinkan.
        self._set_usage(owner_url, granted=False)
        assert has_table(owner_cur, role, "audit_log", "INSERT") is True
        assert has_sequence(owner_cur, role, "audit_log_id_seq", "USAGE") is False

        with process_conn("executor") as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
                cur.execute(insert)
            assert "audit_log_id_seq" in str(exc.value)
            conn.rollback()

        # 3. Kembalikan USAGE: INSERT berhasil lagi.
        self._set_usage(owner_url, granted=True)
        with process_conn("executor") as conn, conn.cursor() as cur:
            cur.execute(insert)
            conn.commit()

    def test_each_owning_process_has_usage_on_its_own_sequence(
        self, owner_cur: psycopg.Cursor, roles: RoleNames
    ) -> None:
        pairs = [
            ("executor", "executions_id_seq"),
            ("watcher", "settlements_id_seq"),
            ("reconciler", "reconciliations_id_seq"),
        ]
        for process, sequence in pairs:
            assert has_sequence(owner_cur, roles.login(process), sequence, "USAGE") is True

    def test_sequence_counter_cannot_be_moved_directly(self, process_conn) -> None:
        """USAGE memberi nextval, bukan setval: counter tidak bisa digeser."""
        problems = []
        for process in PROCESSES:
            with process_conn(process) as conn, conn.cursor() as cur:
                try:
                    cur.execute("SELECT setval('audit_log_id_seq', 1)")
                except psycopg.errors.InsufficientPrivilege:
                    pass
                else:
                    problems.append(f"{process} bisa setval audit_log_id_seq")
                conn.rollback()
        assert problems == [], problems
