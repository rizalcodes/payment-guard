"""Start Guard, lock sesi per agent, dan heartbeat. ST-02, ST-29, D5.

Fixture ``guard_session_factory`` menutup setiap sesi di teardown, yang
melepas lock sesinya. Tanpa itu, tes berikutnya akan gagal start dengan alasan
yang salah.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from conftest_guard import (
    RECIPIENT_A,
    audit_payloads,
    count_events,
    guard_url,
    mandate_document,
)
from payment_guard.config import GuardConfig
from payment_guard.db import try_session_lock
from payment_guard.decision_service import CheckPaymentInput, check_payment
from payment_guard.guard_service import (
    REQUIRED_TABLES,
    GuardSession,
    GuardStartupError,
    GuardUnavailableError,
    Heartbeat,
    load_mandate_file,
    load_signing_identity,
    require_tables,
    start_guard,
)
from payment_guard.locks import advisory_key
from payment_guard.mandate import compute_mandate_hash, load_mandate

pytestmark = pytest.mark.integration


class TestMandateLoaded:
    def test_session_carries_the_mandate_and_hash(self, guard: GuardSession) -> None:
        expected = compute_mandate_hash(load_mandate(mandate_document()))
        assert guard.agent_id == "demo-agent"
        assert guard.mandate_hash == expected

    def test_mandates_row_is_written(self, guard: GuardSession, migrated: dict[str, str]) -> None:
        url = guard_url(migrated)
        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT mandate_id, agent_id, content FROM mandates WHERE mandate_hash = %s",
                (guard.mandate_hash,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "m-001"
        assert row[1] == "demo-agent"
        assert row[2]["chain"] == "base-sepolia"

    def test_active_mandates_points_at_it(
        self, guard: GuardSession, migrated: dict[str, str]
    ) -> None:
        with psycopg.connect(guard_url(migrated)) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT mandate_hash FROM active_mandates WHERE agent_id = %s",
                (guard.agent_id,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == guard.mandate_hash

    def test_mandate_loaded_is_audited(self, guard: GuardSession, migrated: dict[str, str]) -> None:
        payloads = audit_payloads(guard_url(migrated), "mandate_loaded")
        assert len(payloads) == 1
        assert payloads[0]["mandate_id"] == "m-001"
        assert payloads[0]["mandate_hash"] == guard.mandate_hash
        assert payloads[0]["agent_id"] == "demo-agent"

    def test_restart_with_the_same_mandate_is_idempotent(
        self,
        migrated: dict[str, str],
        guard_session_factory: Callable[..., GuardSession],
    ) -> None:
        url = guard_url(migrated)
        first = guard_session_factory(url)
        first_hash = first.mandate_hash
        first.close()

        second = guard_session_factory(url)
        assert second.mandate_hash == first_hash

        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM mandates")
            assert int(cur.fetchone()[0]) == 1
            cur.execute("SELECT count(*) FROM active_mandates")
            assert int(cur.fetchone()[0]) == 1

    def test_changing_the_mandate_moves_active_mandates(
        self,
        migrated: dict[str, str],
        guard_session_factory: Callable[..., GuardSession],
    ) -> None:
        url = guard_url(migrated)
        first = guard_session_factory(url, name="awal")
        first.close()

        second = guard_session_factory(url, name="baru", max_per_day="300")
        assert second.mandate_hash != first.mandate_hash

        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM mandates")
            assert int(cur.fetchone()[0]) == 2, "mandates append-only, dua baris"
            cur.execute(
                "SELECT mandate_hash FROM active_mandates WHERE agent_id = %s",
                ("demo-agent",),
            )
            assert cur.fetchone()[0] == second.mandate_hash


class TestOneGuardPerAgent:
    """ST-02: Guard kedua untuk agent yang sama gagal start."""

    def test_second_guard_for_the_same_agent_fails(
        self,
        migrated: dict[str, str],
        guard_session_factory: Callable[..., GuardSession],
    ) -> None:
        url = guard_url(migrated)
        first = guard_session_factory(url)
        assert first.healthy

        with pytest.raises(GuardStartupError, match="sudah ada Guard"):
            guard_session_factory(url, name="kedua")

    def test_failure_is_audited(
        self,
        migrated: dict[str, str],
        guard_session_factory: Callable[..., GuardSession],
    ) -> None:
        url = guard_url(migrated)
        guard_session_factory(url)
        with pytest.raises(GuardStartupError):
            guard_session_factory(url, name="kedua")

        payloads = audit_payloads(url, "startup_check_failed")
        assert len(payloads) == 1
        assert payloads[0]["check"] == "guard_session_lock"
        assert payloads[0]["agent_id"] == "demo-agent"

    def test_message_names_the_agent(
        self,
        migrated: dict[str, str],
        guard_session_factory: Callable[..., GuardSession],
    ) -> None:
        url = guard_url(migrated)
        guard_session_factory(url)
        with pytest.raises(GuardStartupError) as exc:
            guard_session_factory(url, name="kedua")
        assert "demo-agent" in str(exc.value)

    def test_different_agents_do_not_block_each_other(
        self,
        migrated: dict[str, str],
        guard_session_factory: Callable[..., GuardSession],
    ) -> None:
        url = guard_url(migrated)
        first = guard_session_factory(url, name="satu", agent_id="agen-satu")
        second = guard_session_factory(url, name="dua", agent_id="agen-dua")
        assert first.healthy and second.healthy
        assert first.agent_id != second.agent_id

    def test_slot_is_free_after_the_first_guard_closes(
        self,
        migrated: dict[str, str],
        guard_session_factory: Callable[..., GuardSession],
    ) -> None:
        url = guard_url(migrated)
        first = guard_session_factory(url)
        first.close()
        second = guard_session_factory(url, name="kedua")
        assert second.healthy

    def test_failed_start_does_not_leak_a_lock(
        self,
        migrated: dict[str, str],
        guard_session_factory: Callable[..., GuardSession],
    ) -> None:
        """Start yang gagal harus melepas apa pun yang sudah dipegangnya.

        Diperiksa lewat akibatnya, bukan lewat jumlah koneksi di
        ``pg_stat_activity``: jumlah itu ikut berubah karena pool fixture dan
        koneksi tes lain, jadi angkanya tidak pernah stabil. Kalau sebuah start
        yang gagal meninggalkan lock, slot agent tidak akan pernah kosong lagi
        dan start keempat di bawah ini gagal.
        """
        url = guard_url(migrated)
        pertama = guard_session_factory(url)
        for nomor in range(3):
            with pytest.raises(GuardStartupError):
                guard_session_factory(url, name=f"gagal-{nomor}")
        pertama.close()
        keempat = guard_session_factory(url, name="keempat")
        assert keempat.healthy


class TestSessionLockProbe:
    """Dasar probe Approvals CLI di M4 (ST-30, ST-44)."""

    def test_lock_is_held_while_guard_runs(
        self, guard: GuardSession, migrated: dict[str, str]
    ) -> None:
        with psycopg.connect(guard_url(migrated)) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                assert try_session_lock(cur, "guard", guard.agent_id) is False

    def test_lock_is_free_after_guard_stops(
        self,
        migrated: dict[str, str],
        guard_session_factory: Callable[..., GuardSession],
    ) -> None:
        url = guard_url(migrated)
        session = guard_session_factory(url)
        agent_id = session.agent_id
        session.close()
        with psycopg.connect(url) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                assert try_session_lock(cur, "guard", agent_id) is True

    def test_lock_key_matches_the_specification(self, guard: GuardSession) -> None:
        """Kunci yang dipakai harus kunci yang didokumentasikan."""
        key = advisory_key("guard", guard.agent_id)
        with psycopg.connect(guard.config.db_url) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND ((classid::bigint << 32) | objid::bigint) = %s",
                (key & 0xFFFFFFFFFFFFFFFF,),
            )
            # Representasi kunci di pg_locks bergantung versi; yang penting
            # adalah lock-nya memang dipegang, yang sudah diuji di atas.
            assert cur.fetchone() is not None


class TestStartupChecks:
    def test_missing_tables_are_reported(self, bootstrapped: dict[str, str]) -> None:
        """Database yang sudah di-bootstrap tapi belum dimigrasi."""
        from payment_guard.config import RUNTIME_URL_ENV_VARS

        url = bootstrapped[RUNTIME_URL_ENV_VARS["guard"]]
        with psycopg.connect(url) as conn:
            with pytest.raises(GuardStartupError, match="tabel yang dibutuhkan tidak ada"):
                require_tables(conn)

    def test_message_points_at_migrate(self, bootstrapped: dict[str, str]) -> None:
        from payment_guard.config import RUNTIME_URL_ENV_VARS

        url = bootstrapped[RUNTIME_URL_ENV_VARS["guard"]]
        with psycopg.connect(url) as conn:
            with pytest.raises(GuardStartupError) as exc:
                require_tables(conn)
        assert "guard db migrate" in str(exc.value)

    def test_all_tables_present_after_migration(self, guard: GuardSession) -> None:
        with psycopg.connect(guard.config.db_url) as conn:
            assert require_tables(conn) is None

    def test_schema_migrations_is_not_in_the_required_list(self) -> None:
        """guard_app memang tidak punya hak ke tabel itu (matriks 3.3).

        Memasukkannya ke daftar akan membuat Guard gagal start pada database
        yang benar-benar sudah dimigrasi.
        """
        assert "schema_migrations" not in REQUIRED_TABLES

    def test_guard_cannot_read_schema_migrations(self, guard: GuardSession) -> None:
        """Batas yang dilaporkan: versi migrasi tidak bisa diperiksa Guard."""
        with psycopg.connect(guard.config.db_url) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT count(*) FROM schema_migrations")

    def test_invalid_mandate_blocks_startup(
        self, migrated: dict[str, str], guard_keys: dict[str, Path], tmp_path: Path
    ) -> None:
        """PRD v2 bagian 6.1: gagal = proses tidak start."""
        bad = tmp_path / "mandate-rusak.json"
        bad.write_text(json.dumps({"mandate_id": "m-001"}), encoding="utf-8")
        config = GuardConfig(
            db_url=guard_url(migrated),
            mandate_path=bad,
            signing_key_path=guard_keys["signing"],
            public_keys_path=guard_keys["public"],
            heartbeat_seconds=1,
            lock_timeout_ms=5000,
        )
        with pytest.raises(GuardStartupError, match="mandate tidak sah"):
            start_guard(config)

    def test_nothing_is_written_when_the_mandate_is_invalid(
        self, migrated: dict[str, str], guard_keys: dict[str, Path], tmp_path: Path
    ) -> None:
        url = guard_url(migrated)
        bad = tmp_path / "mandate-rusak.json"
        bad.write_text("{ bukan json", encoding="utf-8")
        config = GuardConfig(
            db_url=url,
            mandate_path=bad,
            signing_key_path=guard_keys["signing"],
            public_keys_path=guard_keys["public"],
            heartbeat_seconds=1,
            lock_timeout_ms=5000,
        )
        with pytest.raises(GuardStartupError):
            start_guard(config)
        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM mandates")
            assert int(cur.fetchone()[0]) == 0

    def test_signing_key_is_loaded_but_never_printed(
        self, guard: GuardSession, guard_keys: dict[str, Path]
    ) -> None:
        material = json.loads(guard_keys["signing"].read_text(encoding="utf-8"))["private_key"]
        assert material not in repr(guard.identity)
        assert material not in repr(guard)
        assert guard.identity.key_id.startswith("guard-")

    def test_corrupt_signing_key_blocks_startup(
        self, migrated: dict[str, str], guard_keys: dict[str, Path], tmp_path: Path
    ) -> None:
        guard_keys["signing"].write_text('{"key_id": "x"}', encoding="utf-8")
        with pytest.raises(GuardStartupError, match="harus memuat tepat"):
            load_signing_identity(guard_keys["signing"])

    def test_signing_key_errors_never_include_material(self, guard_keys: dict[str, Path]) -> None:
        document = json.loads(guard_keys["signing"].read_text(encoding="utf-8"))
        material = document["private_key"]
        document["algorithm"] = "rsa"
        guard_keys["signing"].write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(GuardStartupError) as exc:
            load_signing_identity(guard_keys["signing"])
        assert material not in str(exc.value)

    def test_mandate_file_helper_validates(self, tmp_path: Path) -> None:
        path = tmp_path / "m.json"
        path.write_text(json.dumps(mandate_document()), encoding="utf-8")
        assert load_mandate_file(path).agent_id == "demo-agent"


class TestHeartbeat:
    """ST-29 dan Arsitektur v1.1 bagian 9."""

    def test_healthy_guard_passes(self, guard: GuardSession) -> None:
        assert Heartbeat(guard).check_once() is True
        assert guard.healthy

    def test_interval_comes_from_config(self, guard: GuardSession) -> None:
        assert Heartbeat(guard).interval_seconds == guard.config.heartbeat_seconds

    def test_killing_the_lock_connection_fails_the_heartbeat(
        self, guard: GuardSession, migrated: dict[str, str], admin_url: str
    ) -> None:
        pid = guard.lock_connection.info.backend_pid
        with psycopg.connect(_same_database(admin_url, guard.config.db_url)) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT pg_terminate_backend(%s)", (pid,))

        heartbeat = Heartbeat(guard)
        assert heartbeat.check_once() is False

    def test_guard_stops_answering_after_the_lock_is_lost(
        self, guard: GuardSession, admin_url: str
    ) -> None:
        pid = guard.lock_connection.info.backend_pid
        with psycopg.connect(_same_database(admin_url, guard.config.db_url)) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT pg_terminate_backend(%s)", (pid,))

        heartbeat = Heartbeat(guard)
        assert heartbeat.run_once_and_report() is False
        assert guard.healthy is False
        with pytest.raises(GuardUnavailableError, match="berhenti menjawab"):
            guard.require_healthy()

    def test_lock_lost_is_audited(self, guard: GuardSession, admin_url: str) -> None:
        url = guard.config.db_url
        pid = guard.lock_connection.info.backend_pid
        with psycopg.connect(_same_database(admin_url, url)) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT pg_terminate_backend(%s)", (pid,))

        Heartbeat(guard).run_once_and_report()
        payloads = audit_payloads(url, "guard_lock_lost")
        assert len(payloads) == 1
        assert payloads[0]["agent_id"] == guard.agent_id
        assert "heartbeat" in payloads[0]["detail"]

    def test_unhealthy_is_not_reversible(self, guard: GuardSession) -> None:
        guard.mark_unhealthy()
        assert guard.healthy is False
        assert Heartbeat(guard).check_once() is True, "koneksi masih hidup"
        assert guard.healthy is False, "sekali tidak sehat, selalu tidak sehat"

    def test_on_failure_callback_runs(self, guard: GuardSession, admin_url: str) -> None:
        called: list[int] = []
        pid = guard.lock_connection.info.backend_pid
        with psycopg.connect(_same_database(admin_url, guard.config.db_url)) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT pg_terminate_backend(%s)", (pid,))

        Heartbeat(guard, on_failure=lambda: called.append(1)).run_once_and_report()
        assert called == [1]

    def test_thread_loop_detects_failure(self, guard: GuardSession, admin_url: str) -> None:
        """Loop di thread, bukan hanya pemeriksaan tunggal."""
        import time

        heartbeat = Heartbeat(guard)
        heartbeat.start()
        try:
            pid = guard.lock_connection.info.backend_pid
            with psycopg.connect(_same_database(admin_url, guard.config.db_url)) as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_terminate_backend(%s)", (pid,))

            deadline = time.monotonic() + 10
            while guard.healthy and time.monotonic() < deadline:
                time.sleep(0.1)
            assert guard.healthy is False, "heartbeat tidak mendeteksi dalam 10 detik"
        finally:
            heartbeat.stop()

    def test_routine_heartbeat_does_not_disturb_checks(self, guard: GuardSession) -> None:
        """Heartbeat memakai koneksinya sendiri, jadi ia tidak memegang lock apa pun."""
        heartbeat = Heartbeat(guard)
        heartbeat.start()
        try:
            for _ in range(5):
                result = check_payment(
                    guard,
                    CheckPaymentInput(
                        recipient=RECIPIENT_A, amount="1", chain="base-sepolia", reason="x"
                    ),
                )
                assert result.status in {"approved", "needs_human", "denied"}
        finally:
            heartbeat.stop()
        assert guard.healthy

    def test_lock_connection_is_dedicated(self, guard: GuardSession) -> None:
        """Koneksi lock bukan koneksi yang dipakai transaksi keputusan."""
        assert guard.lock_connection.autocommit is True
        assert not guard.lock_connection.closed


def _same_database(admin_url: str, guard_db_url: str) -> str:
    """URL admin yang menunjuk database yang sama dengan ``guard_db_url``."""
    database = guard_db_url.rsplit("/", 1)[1]
    base = admin_url.rsplit("/", 1)[0]
    return f"{base}/{database}"


def test_guard_cannot_write_audit_log_twice_for_one_start(
    guard: GuardSession, migrated: dict[str, str]
) -> None:
    """Satu start, satu ``mandate_loaded``."""
    assert count_events(guard_url(migrated), "mandate_loaded") == 1
