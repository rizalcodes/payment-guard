"""Fixture bersama untuk tes integrasi Guard (M3).

Diimpor oleh ``conftest.py`` di direktori ini, bukan dipakai langsung, supaya
fixture M0 sampai M2 tetap bisa dibaca tanpa tercampur isi M3.

Aturan yang dijaga di sini:

* Bahan kunci selalu di ``tmp_path``, **di luar** akar repo. Tidak ada tes
  yang menulis ke ``keys/`` atau membuat ``guard_public.json`` di akar repo.
* Setiap sesi Guard ditutup di teardown, yang melepas lock sesinya. Lock yang
  tertinggal akan membuat tes berikutnya gagal start dengan alasan yang salah.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from payment_guard.config import ApprovalsConfig, GuardConfig
from payment_guard.guard_service import GuardSession, start_guard
from payment_guard.keygen import run_keygen

#: Dua alamat dari vektor EIP-55, dipakai di seluruh tes M3.
RECIPIENT_A = "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"
RECIPIENT_B = "0xfb6916095ca1df60bb79ce92ce3ea74c37c5d359"
RECIPIENT_OUTSIDE = "0xdbf03b407c01e7cd3cbea99509d93f8dddc8c6fb"

#: 1 USDC dalam base units, supaya angka di tes terbaca.
USDC = 1_000_000


def mandate_document(**overrides: Any) -> dict[str, Any]:
    """Mandate contoh yang bisa ditimpa per tes."""
    document: dict[str, Any] = {
        "mandate_id": "m-001",
        "agent_id": "demo-agent",
        "chain": "base-sepolia",
        "max_per_tx": "50",
        "max_per_day": "200",
        "human_approval_above": "25",
        "allowed_recipients": [RECIPIENT_A, RECIPIENT_B],
        "retry_cooldown_seconds": 600,
        "aggregation_window_seconds": 3600,
        "decision_ttl_seconds": 900,
        "human_wait_ttl_seconds": 3600,
    }
    document.update(overrides)
    return document


@pytest.fixture
def guard_keys(tmp_path: Path) -> dict[str, Path]:
    """Pasangan kunci di ``tmp_path``, jauh dari akar repo."""
    secrets_dir = tmp_path / "secrets-di-luar-repo"
    signing_path = secrets_dir / "guard_signing.json"
    public_path = tmp_path / "guard_public.json"
    run_keygen(signing_path, public_path)
    return {"signing": signing_path, "public": public_path}


@pytest.fixture
def guard_config_factory(tmp_path: Path, guard_keys: dict[str, Path]) -> Callable[..., GuardConfig]:
    """Pabrik :class:`GuardConfig` yang menulis berkas mandate-nya sendiri."""

    def factory(db_url: str, *, name: str = "mandate", **overrides: Any) -> GuardConfig:
        mandate_path = tmp_path / f"{name}.json"
        mandate_path.write_text(json.dumps(mandate_document(**overrides)), encoding="utf-8")
        return GuardConfig(
            db_url=db_url,
            mandate_path=mandate_path,
            signing_key_path=guard_keys["signing"],
            public_keys_path=guard_keys["public"],
            heartbeat_seconds=1,
            lock_timeout_ms=5000,
        )

    return factory


@pytest.fixture
def guard_session_factory(
    guard_config_factory: Callable[..., GuardConfig],
) -> Iterator[Callable[..., GuardSession]]:
    """Pabrik sesi Guard. Setiap sesi ditutup di teardown."""
    opened: list[GuardSession] = []

    def factory(db_url: str, **overrides: Any) -> GuardSession:
        session = start_guard(guard_config_factory(db_url, **overrides))
        opened.append(session)
        return session

    yield factory

    for session in opened:
        session.close()


@pytest.fixture
def guard(
    migrated: dict[str, str], guard_session_factory: Callable[..., GuardSession]
) -> GuardSession:
    """Satu Guard yang sudah start pada database yang sudah dimigrasi."""
    from payment_guard.config import RUNTIME_URL_ENV_VARS

    return guard_session_factory(migrated[RUNTIME_URL_ENV_VARS["guard"]])


@pytest.fixture
def approvals_config(migrated: dict[str, str], guard_keys: dict[str, Path]) -> ApprovalsConfig:
    """Konfigurasi Approvals CLI untuk database tes.

    URL-nya milik role **approver**, bukan Guard: hanya role itu yang boleh
    menulis ``approved_at``, ``valid_until``, dan ``envelope``. Kuncinya kunci
    yang sama dengan yang dipakai Guard, karena yang menandatangani memang
    tetap Guard (Arsitektur v1.1 bagian 7).
    """
    from payment_guard.config import RUNTIME_URL_ENV_VARS

    return ApprovalsConfig(
        db_url=migrated[RUNTIME_URL_ENV_VARS["approver"]],
        signing_key_path=guard_keys["signing"],
        lock_timeout_ms=5000,
    )


def approver_url(migrated: dict[str, str]) -> str:
    """URL ``approver_app`` dari hasil bootstrap."""
    from payment_guard.config import RUNTIME_URL_ENV_VARS

    return migrated[RUNTIME_URL_ENV_VARS["approver"]]


def guard_url(migrated: dict[str, str]) -> str:
    """URL ``guard_app`` dari hasil bootstrap."""
    from payment_guard.config import RUNTIME_URL_ENV_VARS

    return migrated[RUNTIME_URL_ENV_VARS["guard"]]


def fetch_decision(url: str, decision_id: str) -> dict[str, Any] | None:
    """Baca satu baris ``decisions`` apa adanya, untuk asersi."""
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, agent_id, mandate_hash, chain_id, recipient, amount_base,
                   reason_text, status, reason_code,
                   extract(epoch from created_at)::bigint,
                   extract(epoch from decided_at)::bigint,
                   extract(epoch from approved_at)::bigint,
                   extract(epoch from valid_until)::bigint,
                   extract(epoch from expires_at)::bigint,
                   envelope
            FROM decisions WHERE id = %s
            """,
            (decision_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    columns = (
        "id",
        "agent_id",
        "mandate_hash",
        "chain_id",
        "recipient",
        "amount_base",
        "reason_text",
        "status",
        "reason_code",
        "created_at",
        "decided_at",
        "approved_at",
        "valid_until",
        "expires_at",
        "envelope",
    )
    return dict(zip(columns, row, strict=True))


def insert_decision(
    url: str,
    *,
    agent_id: str,
    mandate_hash: str,
    recipient: str,
    status: str,
    reason_code: str,
    amount_base: int = USDC,
    decided_at_offset: int = 0,
    approved_offset: int | None = None,
    ttl_seconds: int = 900,
) -> str:
    """Sisipkan satu baris ``decisions`` buatan, untuk menyiapkan keadaan.

    Memakai koneksi admin supaya tes bisa membuat baris yang Guard sendiri
    tidak akan pernah menulis, mis. ``rejected_by_human``.
    """
    decision_id = str(uuid.uuid4())
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT extract(epoch from now())::bigint")
        now = int(cur.fetchone()[0])
        decided_at: int | None = now + decided_at_offset
        approved_at: int | None = None
        valid_until: int | None = None
        expires_at: int | None = None
        envelope = None
        if status == "approved":
            # approved_is_complete: approved_at = decided_at, dan envelope wajib.
            approved_at = now + (approved_offset if approved_offset is not None else 0)
            decided_at = approved_at
            valid_until = approved_at + ttl_seconds
            envelope = psycopg.types.json.Jsonb({"payload": {}, "key_id": "x", "signature": "y"})
        elif status == "needs_human":
            # decision_state_timestamps: belum diputus, tapi punya batas tunggu.
            decided_at = None
            expires_at = now + ttl_seconds
        elif status == "expired":
            # decision_state_timestamps: decided_at HARUS sama dengan expires_at.
            expires_at = now + decided_at_offset
            decided_at = expires_at
        # created_at sengaja satu detik lebih awal dari timestamp paling awal:
        # ``expires_after_creation`` menuntut expires_at > created_at, bukan >=.
        created_at = min(x for x in (decided_at, expires_at, now) if x is not None) - 1
        cur.execute(
            """
            INSERT INTO decisions (
              id, agent_id, mandate_hash, chain_id, recipient, amount_base, reason_text,
              status, reason_code,
              created_at, decided_at, approved_at, valid_until, expires_at, envelope
            ) VALUES (
              %s, %s, %s, 84532, %s, %s, %s, %s, %s,
              timestamptz 'epoch' + %s * interval '1 second',
              timestamptz 'epoch' + %s * interval '1 second',
              timestamptz 'epoch' + %s * interval '1 second',
              timestamptz 'epoch' + %s * interval '1 second',
              timestamptz 'epoch' + %s * interval '1 second',
              %s
            )
            """,
            (
                decision_id,
                agent_id,
                mandate_hash,
                recipient,
                amount_base,
                "disiapkan tes",
                status,
                reason_code,
                created_at,
                decided_at,
                approved_at,
                valid_until,
                expires_at,
                envelope,
            ),
        )
        conn.commit()
    return decision_id


def active_mandate_hash(url: str, agent_id: str) -> str | None:
    """Hash mandate aktif untuk satu agent, langsung dari tabel."""
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT mandate_hash FROM active_mandates WHERE agent_id = %s", (agent_id,))
        row = cur.fetchone()
    return None if row is None else row[0]


def count_decisions(url: str, *, status: str | None = None) -> int:
    """Jumlah baris ``decisions``, opsional disaring per status."""
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        if status is None:
            cur.execute("SELECT count(*) FROM decisions")
        else:
            cur.execute("SELECT count(*) FROM decisions WHERE status = %s", (status,))
        return int(cur.fetchone()[0])


def sum_approved(url: str, agent_id: str) -> int:
    """Total base units yang sudah disetujui untuk satu agent."""
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT coalesce(sum(amount_base), 0) FROM decisions "
            "WHERE agent_id = %s AND status = %s",
            (agent_id, "approved"),
        )
        return int(cur.fetchone()[0])


def count_events(url: str, event: str) -> int:
    """Jumlah baris ``audit_log`` untuk satu nama kejadian."""
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_log WHERE event = %s", (event,))
        return int(cur.fetchone()[0])


def audit_payloads(url: str, event: str) -> list[dict[str, Any]]:
    """Payload semua baris ``audit_log`` untuk satu kejadian, terurut waktu."""
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT payload FROM audit_log WHERE event = %s ORDER BY id", (event,))
        return [row[0] for row in cur.fetchall()]
