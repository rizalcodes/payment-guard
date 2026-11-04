"""Fixture integrasi Executor (M5). Tidak menghapus dan tidak merotasi role.

Diimpor oleh ``conftest.py`` di direktori ini.

Mengapa fixture sendiri
-----------------------

Rantai fixture integrasi yang ada (``pristine_db`` -> ``bootstrapped`` ->
``migrated``) melakukan dua hal yang **merusak cluster operator**:

1. ``_drop_managed_roles`` menghapus role ``*_app``.
2. ``run_bootstrap_roles`` dengan berkas env baru tidak bisa membaca password
   lama dari Postgres, jadi ia **menyetel ulang** password role login yang
   sudah ada. Itu membuat ``.env`` operator berhenti cocok.

Keduanya tidak masalah di cluster CI yang bersih, dan memang perlu untuk tes
idempotensi bootstrap. Tapi tes M5 tidak menguji bootstrap sama sekali; ia
butuh satu database bermigrasi dan tidak lebih. Jadi fixture di sini:

* **tidak pernah** menghapus role yang tidak ia buat sendiri,
* **tidak pernah** menyentuh password role mana pun,
* hanya membuat role **grup** bila belum ada — role NOLOGIN tanpa password,
  dibuat lewat blok DO yang idempoten, dan dihapus di teardown hanya bila
  fixture inilah yang membuatnya,
* hanya menghapus database tes yang ia buat sendiri.

Hak akses tanpa password
------------------------

Tes ini perlu menjalankan Executor dengan hak ``executor_rw``, bukan sebagai
admin. Password ``executor_app`` hanya ada di ``.env`` operator, dan tes tidak
boleh membacanya. Karena itu URL-nya memakai ``options=-c role=executor_rw``:
libpq menyetel GUC ``role`` saat sesi dibuka, sehingga ``current_user`` menjadi
role grup itu dan pemeriksaan hak benar-benar berlaku — status superuser tidak
diwarisi lewat ``SET ROLE``.

Mekanisme itu memeriksa dirinya sendiri di :func:`assert_connects_as`, yang
dipanggil fixture. Tanpa itu, kegagalan menyetel role akan membuat tes lolos
dengan hak superuser dan membuktikan lebih sedikit dari yang disangka.
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

from payment_guard.bootstrap import _create_group_role
from payment_guard.config import ExecutorConfig
from payment_guard.mandate import compute_mandate_hash, load_mandate, normalized_form
from payment_guard.migrate import migrations_dir, run_migrate
from payment_guard.privileges import GROUP_ROLES, PROCESSES
from payment_guard.signing import build_payload, sign

#: Awalan database tes M5. Memakai awalan yang sama dengan harness lain supaya
#: pembersih sisa run lama di ``conftest.py`` juga mengenalinya.
M5_DB_PREFIX = "pgtest_m5_"

#: Chain id satu-satunya yang didukung v1.
CHAIN_ID = 84532

#: Alamat USDC testnet Base Sepolia, lowercase. Sama dengan bawaan di config.
USDC_ADDRESS = "0x036cbd53842c5426634e7929541ec2318f3dcf7e"


def _replace_dbname(url: str, dbname: str) -> str:
    base, _, _ = url.rpartition("/")
    return f"{base}/{dbname}"


def role_url(url: str, role: str) -> str:
    """URL yang sama, tapi sesinya terbuka dengan ``role`` sudah disetel.

    Nilainya di-encode: ``-c role=x`` memuat spasi dan tanda sama dengan, yang
    keduanya punya arti di dalam conninfo.
    """
    pemisah = "&" if "?" in url else "?"
    return f"{url}{pemisah}options=-c%20role%3D{role}"


def assert_connects_as(url: str, expected_role: str) -> None:
    """Pastikan URL benar-benar membuka sesi sebagai ``expected_role``.

    Penjaga untuk mekanisme ``options=-c role=...``. Kalau ia gagal tanpa
    suara, seluruh tes hak akses di berkas ini akan berjalan sebagai superuser.
    """
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_user")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == expected_role, (
            f"URL membuka sesi sebagai {row[0]!r}, bukan {expected_role!r}; "
            "mekanisme options=-c role=... tidak berfungsi dan tes hak akses "
            "akan berjalan dengan hak berlebih"
        )


def _ensure_group_roles(conn: psycopg.Connection) -> list[str]:
    """Buat role grup yang belum ada. Mengembalikan yang BARU dibuat saja.

    Role login tidak disentuh sama sekali, dan tidak ada password yang dibuat
    atau diubah. Hanya nama yang dikembalikan di sini yang boleh dihapus di
    teardown.
    """
    wanted = [GROUP_ROLES[p] for p in PROCESSES]
    with conn.cursor() as cur:
        cur.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (wanted,))
        existing = {row[0] for row in cur.fetchall()}
        created = [name for name in wanted if name not in existing]
        for name in created:
            _create_group_role(cur, name)
    return created


@pytest.fixture
def executor_db(maintenance: psycopg.Connection, admin_url: str) -> Iterator[str]:
    """Database tes baru yang sudah dimigrasi penuh, tanpa menyentuh role login.

    Mengembalikan URL admin ke database itu. Migrasi dijalankan oleh role
    admin, jadi pemilik objeknya admin dan bukan ``migrator_owner``. Itu tidak
    mengubah apa pun yang diuji di sini: trigger append-only tetap berlaku, dan
    hak di ``0002_roles.sql`` tetap diberikan ke role grup yang sama.
    """
    created_roles = _ensure_group_roles(maintenance)

    dbname = f"{M5_DB_PREFIX}{secrets.token_hex(6)}"
    with maintenance.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    url = _replace_dbname(admin_url, dbname)
    try:
        applied = run_migrate(url, migrations_dir())
        assert [f.label for f in applied] == ["0001_init.sql", "0002_roles.sql"]
        yield url
    finally:
        with maintenance.cursor() as cur:
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname))
            )
            # Hanya role yang fixture ini buat sendiri. Role yang sudah ada
            # sebelumnya milik operator dan tidak pernah disentuh.
            for name in created_roles:
                cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


@pytest.fixture
def executor_url(executor_db: str) -> str:
    """URL database tes dengan hak ``executor_rw``, bukan hak admin."""
    url = role_url(executor_db, GROUP_ROLES["executor"])
    assert_connects_as(url, GROUP_ROLES["executor"])
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
