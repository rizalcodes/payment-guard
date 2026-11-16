"""Start Guard, lock sesi per agent, dan heartbeat fail-closed.

Arsitektur v1.1 bagian 5.3 (urutan start), bagian 4 (lock), bagian 9
(heartbeat), bagian 13 (mode gagal). Security v1.1 S1, ST-02, ST-29, D5.

Urutan start, dan urutannya penting
-----------------------------------

1. Validasi mandate dari berkas, hitung ``mandate_hash``.
2. Muat bahan kunci penanda tangan dari luar repo.
3. Satu transaksi: lock ``mandate:<agent_id>`` eksklusif, catat mandate,
   perbarui ``active_mandates``, audit ``mandate_loaded``. Commit.
4. **Setelah commit**: koneksi dedikasi, ambil lock sesi ``guard:<agent_id>``
   dengan ``pg_try_advisory_lock``. Gagal berarti sudah ada Guard untuk agent
   ini: audit ``startup_check_failed`` dan tolak start (ST-02).

Langkah 4 memakai koneksi **dedikasi** dan lock **tingkat sesi**, bukan
transaksi: lock itu harus hidup selama Guard hidup, dan harus lepas sendiri
kalau prosesnya mati. Koneksi itu tidak boleh lewat pool atau pgbouncer mode
transaksi, karena lock sesi akan menempel pada koneksi backend yang dipakai
ulang pihak lain (Security v1.1 asumsi 5).

Fail-closed
-----------

Heartbeat memeriksa koneksi lock itu. Begitu gagal, Guard menandai dirinya
tidak sehat, dan setiap jawaban berikutnya ditolak sebelum menyentuh database.
Bendera itu diperiksa **sebelum** setiap jawaban, bukan setelahnya: Guard yang
sudah kehilangan lock tidak boleh menjawab satu permintaan pun lagi, karena
Guard lain mungkin sudah memegang agent yang sama.
"""

from __future__ import annotations

import base64
import binascii
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import psycopg
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg.types.json import Jsonb

from payment_guard.chains import NETWORK_PRODUCTION
from payment_guard.config import GuardConfig
from payment_guard.db import (
    SET_STATEMENT_TIMEOUT,
    MigrationError,
    open_connection,
    take_xact_lock,
    try_session_lock,
)
from payment_guard.events import Actor, AuditError, Event, audit
from payment_guard.mandate import (
    Mandate,
    MandateError,
    compute_mandate_hash,
    load_mandate,
    normalized_form,
)
from payment_guard.signing import ED25519_KEY_SIZE, KEY_ID_PATTERN

#: Tabel yang harus ada sebelum Guard mau menjawab apa pun.
#:
#: ``schema_migrations`` TIDAK ada di daftar ini: ``guard_app`` memang tidak
#: punya hak ke tabel itu (matriks di Arsitektur v1.1 bagian 3.3), jadi Guard
#: tidak bisa memeriksa versi migrasi. Lihat catatan batas di README M3.
REQUIRED_TABLES: Final[tuple[str, ...]] = (
    "mandates",
    "active_mandates",
    "decisions",
    "executions",
    "audit_log",
)

#: SQL statik; nilai selalu lewat parameter.
TABLE_EXISTS: Final = "SELECT to_regclass(%s) IS NOT NULL"
INSERT_MANDATE: Final = (
    "INSERT INTO mandates (mandate_hash, mandate_id, agent_id, content) "
    "VALUES (%s, %s, %s, %s) ON CONFLICT (mandate_hash) DO NOTHING"
)
UPSERT_ACTIVE_MANDATE: Final = (
    "INSERT INTO active_mandates (agent_id, mandate_hash, activated_at) "
    "VALUES (%s, %s, now()) "
    "ON CONFLICT (agent_id) DO UPDATE "
    "SET mandate_hash = EXCLUDED.mandate_hash, activated_at = EXCLUDED.activated_at"
)
HEARTBEAT_PROBE: Final = "SELECT 1"

#: Algoritma yang diterima di berkas kunci. Hanya satu (Security v1.1 E5).
SIGNING_ALGORITHM: Final = "ed25519"

#: Field yang harus ada di berkas kunci penanda tangan.
SIGNING_KEY_FIELDS: Final[frozenset[str]] = frozenset({"key_id", "algorithm", "private_key"})


class GuardStartupError(RuntimeError):
    """Guard menolak start. Pesannya tidak pernah memuat bahan kunci."""


class GuardUnavailableError(RuntimeError):
    """Guard tidak sehat dan tidak akan menjawab.

    Dinaikkan sebelum apa pun menyentuh database, supaya Guard yang sudah
    kehilangan lock tidak bisa menulis keputusan.
    """


@dataclass(frozen=True)
class SigningIdentity:
    """Bahan kunci penanda tangan Guard, beserta ``key_id``-nya.

    ``repr`` sengaja tidak menampilkan kuncinya: objek ini bisa muncul di
    traceback.

    Field-nya bernama ``signer``, dan itu bukan selera. Ketika namanya masih
    ``key``, gitleaks membaca **anotasi tipe di sebelahnya** sebagai rahasia
    ber-entropi tinggi dan menolak commit (aturan ``generic-api-key``, entropi
    3,85). Temuan palsu seperti itu tidak di-allowlist: yang diganti adalah
    nama penyebabnya, supaya pemindai tetap ketat untuk temuan yang sungguhan.
    Dengan alasan yang sama, komentar ini tidak mengutip pola yang memicunya.
    """

    key_id: str
    signer: Ed25519PrivateKey = field(repr=False)


def load_signing_identity(path: Path) -> SigningIdentity:
    """Muat bahan kunci penanda tangan dari berkas JSON.

    Pesan error menyebut berkas dan field yang bermasalah, tapi **tidak pernah**
    isinya.

    Raises:
        GuardStartupError: bila berkasnya tidak bisa dibaca, bentuknya salah,
            algoritmanya bukan Ed25519, atau kuncinya bukan 32 byte.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GuardStartupError(
            f"berkas kunci penanda tangan tidak bisa dibaca ({path}): {exc.strerror}"
        ) from None

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GuardStartupError(
            f"berkas kunci penanda tangan bukan JSON yang sah ({path}): {exc.msg}"
        ) from None

    if not isinstance(document, dict) or set(document) != SIGNING_KEY_FIELDS:
        raise GuardStartupError(
            f"berkas kunci penanda tangan harus memuat tepat: "
            f"{', '.join(sorted(SIGNING_KEY_FIELDS))} ({path})"
        )
    if document["algorithm"] != SIGNING_ALGORITHM:
        raise GuardStartupError(
            f"algoritma kunci harus {SIGNING_ALGORITHM!r}, berkas menyebut "
            f"{document['algorithm']!r} ({path})"
        )

    key_id = document["key_id"]
    if not isinstance(key_id, str) or KEY_ID_PATTERN.match(key_id) is None:
        raise GuardStartupError(f"key_id di berkas kunci tidak sah ({path})")

    material = document["private_key"]
    if not isinstance(material, str):
        raise GuardStartupError(f"bahan kunci harus string base64 ({path})")
    try:
        decoded = base64.b64decode(material, validate=True)
    except (binascii.Error, ValueError):
        raise GuardStartupError(f"bahan kunci bukan base64 standar yang sah ({path})") from None
    if len(decoded) != ED25519_KEY_SIZE:
        raise GuardStartupError(
            f"bahan kunci harus {ED25519_KEY_SIZE} byte, dapat {len(decoded)} byte ({path})"
        )
    try:
        signer = Ed25519PrivateKey.from_private_bytes(decoded)
    except ValueError:
        raise GuardStartupError(f"bahan kunci bukan kunci Ed25519 yang sah ({path})") from None

    return SigningIdentity(key_id=key_id, signer=signer)


def load_mandate_file(path: Path, *, network: str = NETWORK_PRODUCTION) -> Mandate:
    """Baca dan validasi berkas mandate.

    ``network`` menentukan nama chain mana yang sah di mandate; bawaannya
    produksi, jadi pemanggil lama tidak berubah perilakunya.

    Raises:
        GuardStartupError: bila berkasnya tidak terbaca atau mandate-nya tidak
            sah. Guard tidak start dengan mandate yang tidak valid (PRD v2
            bagian 6.1: "gagal = proses tidak start").
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GuardStartupError(
            f"berkas mandate tidak bisa dibaca ({path}): {exc.strerror}"
        ) from None
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GuardStartupError(f"berkas mandate bukan JSON yang sah ({path}): {exc.msg}") from None
    try:
        return load_mandate(document, network=network)
    except MandateError as exc:
        raise GuardStartupError(f"mandate tidak sah ({path}): {exc}") from None


@dataclass
class GuardSession:
    """Guard yang sudah start dan memegang lock sesi untuk satu agent."""

    config: GuardConfig
    mandate: Mandate
    mandate_hash: str
    identity: SigningIdentity
    lock_connection: psycopg.Connection
    _healthy: threading.Event = field(default_factory=threading.Event, repr=False)

    def __post_init__(self) -> None:
        self._healthy.set()

    @property
    def agent_id(self) -> str:
        """Identitas agent, selalu dari mandate (Security v1.1 S1)."""
        return self.mandate.agent_id

    @property
    def healthy(self) -> bool:
        return self._healthy.is_set()

    def require_healthy(self) -> None:
        """Tolak melayani bila lock sesi sudah hilang.

        Raises:
            GuardUnavailableError: bila heartbeat sudah gagal.
        """
        if not self._healthy.is_set():
            raise GuardUnavailableError(
                "Guard kehilangan lock sesinya dan berhenti menjawab. "
                "Jalankan ulang Guard; sampai itu terjadi, tidak ada keputusan "
                "yang dibuat untuk agent ini."
            )

    def mark_unhealthy(self) -> None:
        """Tandai tidak sehat. Tidak pernah bisa dibalik."""
        self._healthy.clear()

    def close(self) -> None:
        """Tutup koneksi lock, yang juga melepas lock sesinya."""
        self.mark_unhealthy()
        try:
            self.lock_connection.close()
        except psycopg.Error:  # pragma: no cover - sudah tertutup
            pass


def require_tables(conn: psycopg.Connection) -> None:
    """Pastikan tabel yang dibutuhkan ada.

    Ini **bukan** pemeriksaan versi migrasi: ``guard_app`` tidak punya hak ke
    ``schema_migrations``, dan menambah hak itu akan melonggarkan matriks hak
    minimum demi kenyamanan. Yang bisa diperiksa dengan hak yang ada adalah
    keberadaan tabel.

    Raises:
        GuardStartupError: bila ada tabel yang tidak ada.
    """
    missing = []
    with conn.cursor() as cur:
        for table in REQUIRED_TABLES:
            cur.execute(TABLE_EXISTS, (table,))
            row = cur.fetchone()
            if row is None or not row[0]:
                missing.append(table)
    if missing:
        raise GuardStartupError(
            f"tabel yang dibutuhkan tidak ada: {', '.join(missing)}. "
            "Jalankan `guard db migrate` lebih dulu."
        )


def start_guard(config: GuardConfig) -> GuardSession:
    """Jalankan urutan start Guard. Mengembalikan sesi yang memegang lock.

    Raises:
        GuardStartupError: bila mandate, kunci, tabel, atau lock sesi gagal.
    """
    mandate = load_mandate_file(config.mandate_path, network=config.network)
    mandate_hash = compute_mandate_hash(mandate)
    identity = load_signing_identity(config.signing_key_path)

    # autocommit=True dengan sengaja: ``conn.transaction()`` pada koneksi yang
    # SUDAH punya transaksi terbuka hanya membuat SAVEPOINT, bukan transaksi
    # sendiri, dan ``close()`` kemudian me-rollback semuanya. Membaca tabel
    # lewat ``require_tables`` cukup untuk membuka transaksi implisit itu.
    # Dengan autocommit, setiap blok ``transaction()`` adalah BEGIN..COMMIT
    # yang sungguhan, apa pun yang terjadi sebelumnya.
    work = open_connection(
        config.db_url,
        autocommit=True,
        lock_timeout_ms=config.lock_timeout_ms,
        statement_timeout_ms=config.lock_timeout_ms,
    )
    lock_connection: psycopg.Connection | None = None
    try:
        require_tables(work)

        # Satu transaksi: mandate tercatat dan menjadi aktif, atau tidak sama
        # sekali. Lock mandate: diambil lebih dulu, sesuai urutan lock di
        # Arsitektur v1.1 bagian 4.
        with work.transaction(), work.cursor() as cur:
            take_xact_lock(cur, "mandate", mandate.agent_id)
            cur.execute(
                INSERT_MANDATE,
                (
                    mandate_hash,
                    mandate.mandate_id,
                    mandate.agent_id,
                    Jsonb(normalized_form(mandate)),
                ),
            )
            cur.execute(UPSERT_ACTIVE_MANDATE, (mandate.agent_id, mandate_hash))
            audit(
                cur,
                Event.MANDATE_LOADED,
                {
                    "mandate_id": mandate.mandate_id,
                    "mandate_hash": mandate_hash,
                    "agent_id": mandate.agent_id,
                },
            )

        # Setelah commit: lock sesi di koneksi dedikasi. Statement timeout
        # dipasang sekarang supaya heartbeat tidak perlu menyetelnya tiap kali.
        lock_connection = open_connection(
            config.db_url,
            autocommit=True,
            statement_timeout_ms=config.heartbeat_seconds * 1000,
        )
        with lock_connection.cursor() as cur:
            acquired = try_session_lock(cur, "guard", mandate.agent_id)

        if not acquired:
            with work.transaction(), work.cursor() as cur:
                audit(
                    cur,
                    Event.STARTUP_CHECK_FAILED,
                    {
                        "check": "guard_session_lock",
                        "agent_id": mandate.agent_id,
                        "detail": "sudah ada Guard lain untuk agent ini",
                    },
                )
            raise GuardStartupError(
                f"sudah ada Guard yang berjalan untuk agent {mandate.agent_id!r}. "
                "Satu agent hanya boleh dilayani satu Guard; hentikan yang lama "
                "sebelum menjalankan yang baru."
            )
    except BaseException:
        if lock_connection is not None:
            lock_connection.close()
        raise
    finally:
        work.close()

    return GuardSession(
        config=config,
        mandate=mandate,
        mandate_hash=mandate_hash,
        identity=identity,
        lock_connection=lock_connection,
    )


class Heartbeat:
    """Pemeriksa koneksi lock, berjalan di thread sendiri.

    Arsitektur v1.1 bagian 9. Begitu pemeriksaan gagal, sesi ditandai tidak
    sehat **sebelum** apa pun lain terjadi, lalu kejadiannya dicatat lewat
    koneksi baru (koneksi lock sudah mati, jadi ia tidak bisa menulis).
    """

    def __init__(
        self,
        session: GuardSession,
        *,
        on_failure: Callable[[], None] | None = None,
    ) -> None:
        self._session = session
        self._on_failure = on_failure
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Nama pengecualian terakhir yang menghalangi audit, bila ada. Untuk
        #: diagnosis dan tes; bukan bagian kontrak.
        self._last_audit_error: str | None = None

    @property
    def interval_seconds(self) -> int:
        return self._session.config.heartbeat_seconds

    def check_once(self) -> bool:
        """Satu pemeriksaan. False berarti koneksi lock sudah hilang."""
        try:
            with self._session.lock_connection.cursor() as cur:
                cur.execute(HEARTBEAT_PROBE)
                row = cur.fetchone()
        except psycopg.Error:
            return False
        return row is not None and row[0] == 1

    def fail(self, detail: str) -> None:
        """Tandai tidak sehat, catat ``guard_lock_lost``, panggil ``on_failure``.

        Urutannya disengaja: bendera lebih dulu, supaya tidak ada permintaan
        yang terlayani selama audit sedang ditulis.
        """
        self._session.mark_unhealthy()
        self._record_lock_lost(detail)
        if self._on_failure is not None:
            self._on_failure()

    def _record_lock_lost(self, detail: str) -> None:
        """Catat kehilangan lock lewat koneksi baru, bila masih bisa.

        Koneksi lock sudah mati, jadi audit tidak bisa ditulis di sana. Kalau
        database juga tidak terjangkau, kejadiannya memang tidak bisa dicatat;
        itu batas yang jujur, bukan alasan menunda berhenti menjawab.
        """
        # Setiap kegagalan di sini ditelan dengan sengaja. Sesi sudah ditandai
        # tidak sehat sebelum fungsi ini dipanggil, jadi Guard sudah berhenti
        # menjawab; mencatat kejadiannya adalah tambahan, bukan prasyarat.
        try:
            connection = open_connection(
                self._session.config.db_url,
                statement_timeout_ms=self._session.config.heartbeat_seconds * 1000,
            )
        except (psycopg.Error, MigrationError, OSError):
            self._last_audit_error = "tidak bisa menyambung"
            return
        try:
            with connection.transaction(), connection.cursor() as cur:
                audit(
                    cur,
                    Event.GUARD_LOCK_LOST,
                    {
                        "agent_id": self._session.agent_id,
                        "detail": detail,
                    },
                    actor=Actor.GUARD,
                )
        except (psycopg.Error, AuditError, OSError) as exc:
            self._last_audit_error = type(exc).__name__
        finally:
            connection.close()

    def run_once_and_report(self) -> bool:
        """Periksa sekali; bila gagal, jalankan penanganannya. True = sehat."""
        if self.check_once():
            return True
        self.fail("heartbeat gagal pada koneksi lock")
        return False

    def start(self) -> None:
        """Mulai thread heartbeat."""
        if self._thread is not None:  # pragma: no cover - penjaga pemakaian
            raise RuntimeError("heartbeat sudah berjalan")
        self._thread = threading.Thread(
            target=self._loop, name=f"guard-heartbeat-{self._session.agent_id}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: int = 10) -> None:
        """Minta thread berhenti dan tunggu."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            if not self.run_once_and_report():
                return


def set_statement_timeout(cur: psycopg.Cursor, milliseconds: int) -> None:
    """Pasang ``statement_timeout`` pada sesi kursor ini."""
    cur.execute(SET_STATEMENT_TIMEOUT, (str(milliseconds),))
