"""Fixture integrasi Executor (M5). Diimpor oleh ``conftest.py`` di direktori ini.

Executor dijalankan sebagai role login ``*_executor_app`` yang sebenarnya,
bukan sebagai admin, supaya pemeriksaan hak di ``0002_roles.sql`` benar-benar
berlaku. :func:`assert_connects_as` yang memastikannya: tanpa itu, tes hak
akses bisa lolos dengan hak berlebih dan membuktikan lebih sedikit dari yang
disangka.

Versi M5 berkas ini membangun database dan role-nya sendiri, karena rantai
``pristine_db`` saat itu menghapus role ``*_app`` dan menyetel ulang
password-nya di cluster yang sama dengan instalasi operator. Sejak M5.1 harness
berjalan di cluster tes terpisah dengan nama role ber-awalan acak, jadi fixture
di sini kembali memakai rantai biasa dan semua jalan khusus itu hilang. Lihat
kepala ``conftest.py``.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from payment_guard.config import ExecutorConfig
from payment_guard.mandate import compute_mandate_hash, load_mandate, normalized_form
from payment_guard.signing import build_payload, sign

#: Chain id satu-satunya yang didukung v1.
CHAIN_ID = 84532

#: Alamat USDC testnet Base Sepolia, lowercase. Sama dengan bawaan di config.
USDC_ADDRESS = "0x036cbd53842c5426634e7929541ec2318f3dcf7e"


def assert_connects_as(url: str, expected_role: str) -> None:
    """Pastikan URL benar-benar membuka sesi sebagai ``expected_role``.

    Penjaga untuk seluruh tes hak akses: kalau URL-nya ternyata milik admin,
    tes di bawahnya akan lolos dengan hak berlebih dan membuktikan lebih
    sedikit dari yang disangka.
    """
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_user")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == expected_role, (
            f"URL membuka sesi sebagai {row[0]!r}, bukan {expected_role!r}; "
            "tes hak akses akan berjalan dengan hak yang salah"
        )


@pytest.fixture
def executor_db(pristine_db: str, migrated: dict[str, str]) -> str:
    """URL **admin** ke database tes yang sudah dimigrasi penuh.

    Dipakai untuk menyiapkan keadaan dan untuk membaca ``audit_log``, yang
    role executor sendiri tidak boleh baca (``0002_roles.sql`` hanya memberinya
    INSERT di tabel itu).
    """
    return pristine_db


@pytest.fixture
def executor_url(migrated: dict[str, str], roles: Any) -> str:
    """URL role login Executor yang sebenarnya, bukan URL admin."""
    from payment_guard.config import RUNTIME_URL_ENV_VARS

    url = migrated[RUNTIME_URL_ENV_VARS["executor"]]
    assert_connects_as(url, roles.login("executor"))
    return url


@pytest.fixture
def executor_config(
    executor_url: str, guard_keys: dict[str, Path], tmp_path: Path
) -> ExecutorConfig:
    """Konfigurasi Executor untuk tes.

    Kunci wallet dan registri kunci publik keduanya di ``tmp_path``, di luar
    akar repo. Kunci penanda tangan Guard **tidak** masuk ke sini: Executor
    tidak punya field untuknya.
    """
    from fake_chain import write_test_wallet

    return ExecutorConfig(
        db_url=executor_url,
        wallet_key_path=write_test_wallet(tmp_path / "di-luar-repo"),
        public_keys_path=guard_keys["public"],
        rpc_url="http://127.0.0.1:1/tidak-pernah-dihubungi",
        chain_id=CHAIN_ID,
        usdc_address=USDC_ADDRESS,
        lock_timeout_ms=5000,
    )


# --------------------------------------------------------------------------- #
# Menyiapkan keadaan
# --------------------------------------------------------------------------- #


def seed_mandate(url: str, *, agent_id: str, mandate_id: str = "m-001", **overrides: Any) -> str:
    """Sisipkan satu mandate dan jadikan ia mandate aktif agent itu.

    Isi kolom ``content`` adalah bentuk **ternormalisasi**, sama dengan yang
    ditulis Guard: jumlahnya base units, bukan desimal USDC.
    """
    from conftest_guard import mandate_document

    document = mandate_document(agent_id=agent_id, mandate_id=mandate_id, **overrides)
    mandate = load_mandate(document)
    mandate_hash = compute_mandate_hash(mandate)
    content = normalized_form(mandate)

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mandates (mandate_hash, mandate_id, agent_id, content) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (mandate_hash, mandate_id, agent_id, psycopg.types.json.Jsonb(content)),
        )
        cur.execute(
            "INSERT INTO active_mandates (agent_id, mandate_hash) VALUES (%s, %s) "
            "ON CONFLICT (agent_id) DO UPDATE SET mandate_hash = EXCLUDED.mandate_hash",
            (agent_id, mandate_hash),
        )
        conn.commit()
    return mandate_hash


def set_active_mandate(url: str, *, agent_id: str, mandate_hash: str) -> None:
    """Ganti mandate aktif agent tanpa menyentuh baris ``decisions``.

    Dipakai untuk menguji cek 4b: envelope yang sah atas aturan yang sudah
    tidak berlaku.
    """
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO active_mandates (agent_id, mandate_hash) VALUES (%s, %s) "
            "ON CONFLICT (agent_id) DO UPDATE SET mandate_hash = EXCLUDED.mandate_hash",
            (agent_id, mandate_hash),
        )
        conn.commit()


def seed_approved_decision(
    url: str,
    *,
    agent_id: str,
    mandate_hash: str,
    recipient: str,
    amount_base: int,
    signing_key_path: Path,
    mandate_id: str = "m-001",
    ttl_seconds: int = 900,
    age_seconds: int = 0,
    envelope_overrides: dict[str, Any] | None = None,
) -> str:
    """Sisipkan satu keputusan ``approved`` beserta envelope bertanda tangannya.

    Envelope-nya **ditandatangani sungguhan** dengan kunci uji, dan setiap
    field-nya dibuat cocok dengan kolom barisnya, supaya cek 4 lolos kecuali
    bila tes memang sengaja membuatnya tidak cocok lewat ``envelope_overrides``.

    ``age_seconds`` menggeser keputusan ke masa lalu. Dengan
    ``age_seconds > ttl_seconds`` envelope-nya sudah kedaluwarsa menurut jam
    database, yang dipakai menguji cek 3b.
    """
    from payment_guard.guard_service import load_signing_identity

    identity = load_signing_identity(signing_key_path)
    decision_id = str(uuid.uuid4())

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT extract(epoch from now())::bigint")
        row = cur.fetchone()
        assert row is not None
        approved_at = int(row[0]) - age_seconds
        valid_until = approved_at + ttl_seconds

        nilai: dict[str, Any] = {
            "decision_id": decision_id,
            "mandate_id": mandate_id,
            "agent_id": agent_id,
            "recipient": recipient,
            "amount_base": amount_base,
            "issued_at": approved_at,
            "valid_until": valid_until,
            "mandate_hash": mandate_hash,
        }
        nilai.update(envelope_overrides or {})
        envelope = sign(build_payload(**nilai), identity.signer, identity.key_id)

        cur.execute(
            """
            INSERT INTO decisions (
              id, agent_id, mandate_hash, chain_id, recipient, amount_base, reason_text,
              status, reason_code, created_at, decided_at, approved_at, valid_until, envelope
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, 'approved', 'approved_by_human',
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
                CHAIN_ID,
                recipient,
                amount_base,
                "disiapkan tes",
                approved_at - 1,
                approved_at,
                approved_at,
                valid_until,
                psycopg.types.json.Jsonb(envelope.as_dict()),
            ),
        )
        conn.commit()
    return decision_id


# --------------------------------------------------------------------------- #
# Pembacaan untuk asersi
# --------------------------------------------------------------------------- #


def fetch_execution(url: str, decision_id: str) -> dict[str, Any] | None:
    """Baris ``executions`` apa adanya, atau ``None``."""
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT decision_id::text, tx_hash, from_addr, to_addr, amount_base, nonce,
                   raw_tx, status,
                   broadcast_at IS NOT NULL,
                   extract(epoch from created_at)::bigint,
                   extract(epoch from updated_at)::bigint
            FROM executions WHERE decision_id = %s
            """,
            (decision_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    kolom = (
        "decision_id",
        "tx_hash",
        "from_addr",
        "to_addr",
        "amount_base",
        "nonce",
        "raw_tx",
        "status",
        "has_broadcast_at",
        "created_at",
        "updated_at",
    )
    return dict(zip(kolom, row, strict=True))


def count_executions(url: str) -> int:
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM executions")
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def audit_events(url: str) -> list[tuple[str, str]]:
    """``(actor, event)`` semua baris ``audit_log``, terurut.

    Dibaca lewat URL **admin**: ``executor_rw`` hanya punya INSERT pada
    ``audit_log`` (``0002_roles.sql``), dan itu memang disengaja — proses yang
    menulis bukti tidak perlu bisa membacanya kembali.
    """
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT actor, event FROM audit_log ORDER BY id")
        return [(row[0], row[1]) for row in cur.fetchall()]


def audit_payloads_for(url: str, event: str) -> list[dict[str, Any]]:
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT payload FROM audit_log WHERE event = %s ORDER BY id", (event,))
        return [row[0] for row in cur.fetchall()]


def dump_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)
