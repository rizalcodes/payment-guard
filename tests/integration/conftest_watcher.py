"""Fixture Watcher dan Reconciler. Tidak menghapus role, tidak menyentuh password.

Mengikuti pola ``conftest_executor.py``: URL **admin** dipakai untuk menyiapkan
keadaan dan untuk membaca tabel yang role-nya sendiri tidak boleh baca
(``audit_log`` untuk Watcher), sedangkan URL role login dipakai untuk
menjalankan kode yang diuji. Keduanya dipisah dengan sengaja — tes yang
menjalankan Watcher sebagai admin akan lulus walau GRANT-nya salah, dan
pemisahan hak adalah salah satu hal yang paling ingin dibuktikan di sini.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from conftest_executor import CHAIN_ID, USDC_ADDRESS, assert_connects_as
from fake_chain import TEST_WALLET_ADDRESS
from payment_guard.config import ReconcilerConfig, WatcherConfig

#: Kedalaman konfirmasi untuk tes: kecil supaya blok tidak perlu dibuat belasan,
#: tapi bukan 1 — ``pending`` yang langsung menjadi ``final`` tidak menguji
#: apa pun tentang kedalaman.
TEST_CONFIRMATIONS = 3

#: Jendela mundur K. Harus lebih besar dari kedalaman konfirmasi; loader
#: menolak kalau tidak, dan tes di berkas ini bergantung pada baris ``final``
#: masih berada di dalam jendela.
TEST_REORG_LOOKBACK = 50

#: Batas waktu ``executed_not_settled``. Tes yang perlu melewatinya memakai
#: ``deadline_seconds=0``-nya sendiri lewat ``dataclasses.replace``.
TEST_DEADLINE_SECONDS = 3600


@pytest.fixture
def watcher_url(migrated: dict[str, str], roles: Any) -> str:
    """URL role login Watcher yang sebenarnya, bukan URL admin."""
    from payment_guard.config import RUNTIME_URL_ENV_VARS

    url = migrated[RUNTIME_URL_ENV_VARS["watcher"]]
    assert_connects_as(url, roles.login("watcher"))
    return url


@pytest.fixture
def reconciler_url(migrated: dict[str, str], roles: Any) -> str:
    """URL role login Reconciler yang sebenarnya."""
    from payment_guard.config import RUNTIME_URL_ENV_VARS

    url = migrated[RUNTIME_URL_ENV_VARS["reconciler"]]
    assert_connects_as(url, roles.login("reconciler"))
    return url


@pytest.fixture
def watcher_config(watcher_url: str) -> WatcherConfig:
    """Konfigurasi Watcher untuk tes.

    ``rpc_url`` menunjuk port yang tidak dilayani siapa pun: setiap tes
    memberi :class:`fake_chain.FakeChain`, dan URL ini hanya ada supaya
    kelalaian memberi klien tiruan gagal cepat alih-alih menyentuh jaringan.
    """
    return WatcherConfig(
        db_url=watcher_url,
        rpc_url="http://127.0.0.1:1/tidak-pernah-dihubungi",
        chain_id=CHAIN_ID,
        usdc_address=USDC_ADDRESS,
        wallet_address=TEST_WALLET_ADDRESS,
        confirmations=TEST_CONFIRMATIONS,
        scan_batch_size=100,
        reorg_lookback=TEST_REORG_LOOKBACK,
        interval_seconds=1,
        lock_timeout_ms=5000,
    )


@pytest.fixture
def reconciler_config(reconciler_url: str) -> ReconcilerConfig:
    return ReconcilerConfig(
        db_url=reconciler_url,
        chain_id=CHAIN_ID,
        deadline_seconds=TEST_DEADLINE_SECONDS,
        lock_timeout_ms=5000,
    )


# --------------------------------------------------------------------------- #
# Membaca keadaan lewat URL admin
# --------------------------------------------------------------------------- #


def settlement_rows(url: str) -> list[dict[str, Any]]:
    """Semua baris ``settlements``, terurut, sebagai dict."""
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, chain_id, tx_hash, log_index, block_number, block_hash, "
            "from_addr, to_addr, amount_base, status, finalized_at, last_verified_at "
            "FROM settlements ORDER BY id"
        )
        names = [d.name for d in cur.description or ()]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def reconciliation_rows(url: str) -> list[dict[str, Any]]:
    """Semua baris ``reconciliations``, terurut, sebagai dict."""
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT subject_type, subject_id, state, decision_id::text, "
            "execution_id, settlement_id, detail "
            "FROM reconciliations ORDER BY subject_type, subject_id"
        )
        names = [d.name for d in cur.description or ()]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def cursor_value(url: str, chain_id: int = CHAIN_ID) -> int | None:
    """``watcher_state.last_scanned``, atau ``None`` bila belum ada barisnya."""
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT last_scanned FROM watcher_state WHERE chain_id = %s", (chain_id,))
        row = cur.fetchone()
        return None if row is None else int(row[0])


def set_cursor(url: str, last_scanned: int, chain_id: int = CHAIN_ID) -> None:
    """Setel kursor langsung, untuk menyiapkan keadaan."""
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO watcher_state (chain_id, last_scanned) VALUES (%s, %s) "
            "ON CONFLICT (chain_id) DO UPDATE SET last_scanned = EXCLUDED.last_scanned",
            (chain_id, last_scanned),
        )
        conn.commit()


def table_snapshot(url: str, table: str) -> list[tuple[Any, ...]]:
    """Isi satu tabel apa adanya, untuk membandingkan sebelum dan sesudah.

    Daftar nama tabel dibatasi: nilainya masuk ke SQL sebagai pengenal, dan
    pengenal tidak bisa lewat parameter. Daftar-izin membuatnya tetap statik.
    """
    allowed = {"decisions", "executions", "mandates", "active_mandates"}
    if table not in allowed:
        raise AssertionError(f"tabel {table!r} tidak ada di daftar-izin snapshot")
    statements = {
        "decisions": "SELECT id, status, reason_code, valid_until FROM decisions ORDER BY id",
        "executions": (
            "SELECT id, decision_id, tx_hash, nonce, status, broadcast_at "
            "FROM executions ORDER BY id"
        ),
        "mandates": "SELECT mandate_hash, mandate_id, agent_id FROM mandates ORDER BY mandate_hash",
        "active_mandates": "SELECT agent_id, mandate_hash FROM active_mandates ORDER BY agent_id",
    }
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(statements[table])
        return [tuple(row) for row in cur.fetchall()]
