"""Executor: daftar cek, write-ahead, broadcast, dan recovery idempoten.

PRD v2 bagian 7 dan 9, PRD v2.1 bagian 9, Arsitektur v1.1 bagian 4, 6, dan 8.

Yang dijaga modul ini ada tiga, dan ketiganya saling menopang.

**Executor tidak bisa menyetujui apa pun.** Ia hanya memegang kunci publik
Guard, jadi ia bisa memeriksa pernyataan "pembayaran ini disetujui" tanpa bisa
membuatnya. Nama variabel kunci penanda tangan Guard tidak muncul di berkas
ini, dan ada tes di ``test_source_hygiene.py`` yang memeriksa justru itu —
sebuah nama variabel environment bisa dipakai tanpa impor apa pun, jadi yang
diperiksa adalah teksnya, bukan impornya. Kunci yang Executor pegang adalah
kunci wallet-nya sendiri, yang hanya bisa membelanjakan uang yang sudah
disetujui orang lain.

**Daftar cek urutannya tetap, dan gagal di mana pun berarti berhenti.** Tidak
ada efek samping sampai cek 5 lolos: sebelum itu Executor hanya membaca.
Alasan penolakannya daftar tertutup (:data:`REJECTION_REASONS`), dicatat ke
``audit_log`` sebagai ``execution_rejected``. Jenis cek yang gagal ada di
payload, bukan di nama kejadian: nama kejadian yang mengkodekan jenis akan
berarti satu nama baru setiap kali daftar cek bertambah, dan daftar kejadian
berhenti bisa dipakai sebagai daftar.

**Write-ahead mendahului jaringan.** Transaksi ditandatangani secara lokal,
sehingga ``tx_hash``, ``nonce``, dan ``raw_tx`` sudah diketahui sebelum ada
apa pun yang terkirim. Ketiganya ditulis ke ``executions`` dan di-commit
**sebelum** broadcast. Crash sesudah itu tidak kehilangan jejak: baris
``signed`` memberi :func:`recover_pending` cukup bahan untuk menyiarkan ulang
byte yang sama, yang menghasilkan ``tx_hash`` yang sama, yang karena
``UNIQUE(decision_id)`` dan sifat nonce tidak bisa menjadi dua pembayaran.

Urutan lock, dari Arsitektur v1.1 bagian 4
------------------------------------------

``mandate:<agent_id>`` **shared** diambil sebagai tindakan database pertama di
dalam transaksi, sebelum tabel apa pun dibaca. Guard start memegangnya
eksklusif, jadi tidak ada eksekusi yang bisa membaca ``active_mandates`` di
tengah pergantian mandate. Sesudah cek 4b lolos, ``executor:<from_addr>``
eksklusif diambil sebelum nonce ditentukan: hanya satu penulis nonce per
wallet.

Apa yang **tidak** dilakukan recovery
-------------------------------------

Recovery tidak memverifikasi ulang envelope, mandate, atau ``valid_until``
(PRD v2.1 bagian 9). Baris ``signed`` dan ``broadcast`` sudah dikomit; uang
mungkin sudah bergerak. Satu-satunya pertanyaan yang tersisa adalah apa yang
sebenarnya terjadi di chain. Karena itu ``failed`` hanya ditulis bila receipt
revert atau nonce-nya dipakai transaksi lain. **Timeout bukan kegagalan**:
menandai ``failed`` karena lambat akan menyembunyikan pembayaran yang
sesungguhnya berhasil.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, TextIO

import psycopg

from payment_guard.addresses import to_checksum_address
from payment_guard.chain import (
    ChainClient,
    ChainError,
    Fees,
    Receipt,
    SignedTransaction,
    StartupReport,
    TransactionPlan,
    WalletIdentity,
    Web3Client,
    check_startup,
    encode_transfer,
    load_wallet,
)
from payment_guard.config import ExecutorConfig
from payment_guard.db import (
    connect,
    db_now,
    endpoint_host,
    take_xact_lock,
    take_xact_lock_shared,
    to_int,
)
from payment_guard.events import Event, audit
from payment_guard.money import BaseUnits, base_units, format_amount, format_base_units
from payment_guard.reasons import Status
from payment_guard.signing import (
    ENVELOPE_VERSION,
    SIGNED_STATUS,
    Envelope,
    EnvelopeError,
    Payload,
    PublicKeyEntry,
    VerificationError,
    check_not_expired,
    parse_envelope,
    parse_public_keys,
    verify,
)

# --------------------------------------------------------------------------- #
# Konstanta
# --------------------------------------------------------------------------- #

#: Status baris ``executions``. Sama dengan CHECK di ``0001_init.sql``.
STATUS_SIGNED: Final = "signed"
STATUS_BROADCAST: Final = "broadcast"
STATUS_CONFIRMED: Final = "confirmed"
STATUS_FAILED: Final = "failed"

#: Status yang belum selesai, dan karena itu urusan :func:`recover_pending`.
PENDING_STATUSES: Final[tuple[str, ...]] = (STATUS_SIGNED, STATUS_BROADCAST)

#: Alasan penolakan, daftar tertutup dari PRD v2.1 bagian 9 tabel cek.
REASON_SIGNATURE_INVALID: Final = "signature_invalid"
REASON_VERSION_OR_CHAIN_MISMATCH: Final = "version_or_chain_mismatch"
REASON_NOT_APPROVED: Final = "not_approved"
REASON_EXPIRED: Final = "expired"
REASON_DECISION_MISMATCH: Final = "decision_mismatch"
REASON_MANDATE_NOT_ACTIVE: Final = "mandate_not_active"
REASON_ALREADY_EXECUTED: Final = "already_executed"

#: Urutan cek, dan karena itu urutan alasan yang bisa muncul.
REJECTION_REASONS: Final[tuple[str, ...]] = (
    REASON_SIGNATURE_INVALID,
    REASON_VERSION_OR_CHAIN_MISMATCH,
    REASON_NOT_APPROVED,
    REASON_EXPIRED,
    REASON_DECISION_MISMATCH,
    REASON_ALREADY_EXECUTED,
    REASON_MANDATE_NOT_ACTIVE,
)

#: Sebab ``execution_failed``. Daftar tertutup, dan keduanya berarti "chain
#: sudah menjawab". Tidak ada sebab "timeout" dengan sengaja.
CAUSE_RECEIPT_REVERTED: Final = "receipt_reverted"
CAUSE_NONCE_TAKEN: Final = "nonce_taken"
FAILURE_CAUSES: Final[tuple[str, ...]] = (CAUSE_RECEIPT_REVERTED, CAUSE_NONCE_TAKEN)

#: Hasil satu perintah, untuk dibaca operator dan tes.
OUTCOME_REJECTED: Final = "rejected"

#: Saldo gas minimum yang dianjurkan: 0,001 ETH. Di bawah ini Executor tetap
#: start, hanya memberi peringatan; lihat :class:`payment_guard.chain.StartupReport`.
MINIMUM_GAS_BALANCE_WEI: Final = 10**15

#: ``value`` transaksi selalu nol: yang dipindahkan adalah token ERC-20, bukan
#: ether. Nilai bukan nol di sini akan mengirim ETH ke kontrak token.
TRANSFER_VALUE_WEI: Final = 0

#: Panjang UUID kanonik.
_UUID_LENGTH: Final = 36

# --------------------------------------------------------------------------- #
# SQL statik. Nilai selalu lewat parameter.
# --------------------------------------------------------------------------- #

#: ``envelope::text`` dan bukan kolom JSONB-nya: parser ketat di
#: :func:`payment_guard.signing.parse_envelope` bekerja atas teks, jadi
#: envelope tidak pernah melewati dua pembaca JSON yang berbeda.
SELECT_ENVELOPE: Final = """
SELECT envelope::text, status
FROM decisions
WHERE id = %s
"""

SELECT_DECISION_FOR_EXECUTION: Final = """
SELECT agent_id, recipient, amount_base, mandate_hash, chain_id, status,
       extract(epoch from valid_until)::bigint
FROM decisions
WHERE id = %s
"""

SELECT_ACTIVE_MANDATE_HASH: Final = """
SELECT mandate_hash FROM active_mandates WHERE agent_id = %s
"""

SELECT_EXECUTION_STATUS: Final = """
SELECT status, tx_hash FROM executions WHERE decision_id = %s
"""

SELECT_EXECUTION_ROW: Final = """
SELECT decision_id::text, tx_hash, from_addr, nonce, raw_tx, status
FROM executions
WHERE decision_id = %s
"""

SELECT_PENDING_EXECUTIONS: Final = """
SELECT decision_id::text, tx_hash, from_addr, nonce, raw_tx, status
FROM executions
WHERE status = ANY(%s)
ORDER BY id
"""

#: Nonce tertinggi yang pernah **kami** alokasikan untuk sebuah wallet.
#:
#: Semua status ikut dihitung, termasuk ``failed``: nonce yang sudah dipakai
#: transaksi yang gagal tetap tidak boleh dipakai lagi oleh kami, karena
#: transaksi lama bisa saja masih beredar di mempool.
#:
#: ``coalesce(..., -1)`` ditulis eksplisit, dan itu bukan kosmetik. Nonce
#: pertama yang sah adalah **0**, jadi "belum ada nonce terpakai" tidak bisa
#: diwakili oleh 0 — ia harus diwakili oleh -1, supaya ``+ 1`` di
#: :func:`next_nonce` berlaku seragam untuk tabel kosong maupun tidak. Tanpa
#: ini, cabang tabel-kosong perlu aritmetikanya sendiri, dan cabang itulah yang
#: paling jarang dilewati sehingga paling mudah salah.
SELECT_MAX_NONCE: Final = """
SELECT coalesce(max(nonce), -1) FROM executions WHERE from_addr = %s
"""

#: Nilai yang mewakili "wallet ini belum pernah memakai nonce apa pun".
#:
#: Bukan 0, karena 0 adalah nonce yang sah dan terpakai.
NO_NONCE_USED: Final = -1

#: ``created_at`` dan ``updated_at`` memakai DEFAULT ``now()``: tidak ada CHECK
#: di ``executions`` yang membandingkannya dengan nilai turunan epoch, jadi
#: tidak perlu aritmetika epoch seperti di ``decisions``.
INSERT_EXECUTION: Final = """
INSERT INTO executions (
  decision_id, tx_hash, from_addr, to_addr, amount_base, nonce, raw_tx, status
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""

UPDATE_EXECUTION_BROADCAST: Final = """
UPDATE executions
SET status = %s, broadcast_at = now(), updated_at = now()
WHERE decision_id = %s AND status = %s
"""

UPDATE_EXECUTION_STATUS: Final = """
UPDATE executions
SET status = %s, updated_at = now()
WHERE decision_id = %s AND status = ANY(%s)
"""


class ExecutorError(RuntimeError):
    """Perintah Executor gagal. Pesannya ditujukan ke operator.

    Tidak pernah memuat bahan kunci wallet maupun URL RPC utuh.
    """


class CheckRejected(RuntimeError):
    """Salah satu dari cek 1..5 gagal. Tidak ada transaksi yang dibangun.

    ``reason`` berasal dari :data:`REJECTION_REASONS` dan itulah yang masuk
    ``audit_log``. ``detail`` untuk mata operator dan **tidak** masuk audit:
    ia bisa memuat nilai dari envelope, dan payload audit tidak menerima teks
    yang asalnya dari luar (Security v1.1 I3).
    """

    def __init__(self, reason: str, detail: str) -> None:
        if reason not in REJECTION_REASONS:
            raise ExecutorError(f"alasan penolakan tidak dikenal: {reason!r}")
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------- #
# Bentuk hasil
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExecutionPlan:
    """Hasil cek 1..5: apa yang boleh dieksekusi, dan kapan diperiksa.

    Hanya field di sini yang boleh membentuk transaksi (cek 6). ``now`` adalah
    jam **database** saat cek berjalan, bukan jam proses.
    """

    decision_id: str
    agent_id: str
    recipient: str
    amount_base: BaseUnits
    mandate_hash: str
    chain_id: int
    valid_until: int
    now: int


@dataclass(frozen=True)
class ExecutionOutcome:
    """Hasil satu eksekusi atau satu pemulihan."""

    decision_id: str
    status: str
    tx_hash: str | None
    detail: str


@dataclass(frozen=True)
class PendingExecution:
    """Baris ``executions`` yang belum selesai, apa adanya dari database."""

    decision_id: str
    tx_hash: str
    from_addr: str
    nonce: int
    raw_tx: bytes = field(repr=False)
    status: str


@dataclass(frozen=True)
class ExecutorRuntime:
    """Konfigurasi, klien chain, wallet, dan registri kunci publik.

    Dikumpulkan menjadi satu objek supaya tes bisa menyuntikkan klien tiruan
    tanpa menyentuh jaringan, dan supaya tidak ada fungsi yang perlu membuka
    berkas kunci sendiri.
    """

    config: ExecutorConfig
    client: ChainClient
    wallet: WalletIdentity
    registry: Mapping[str, PublicKeyEntry]


# --------------------------------------------------------------------------- #
# Start
# --------------------------------------------------------------------------- #


def open_runtime(config: ExecutorConfig, *, client: ChainClient | None = None) -> ExecutorRuntime:
    """Muat wallet dan registri kunci publik, dan siapkan klien RPC.

    Args:
        client: klien pengganti, dipakai tes supaya tidak ada jaringan yang
            tersentuh. Bila kosong, :class:`payment_guard.chain.Web3Client`
            dibuat dari ``config.rpc_url``.

    Raises:
        ExecutorError: bila registri kunci publik tidak bisa dibaca.
        ChainError: bila berkas kunci wallet tidak sah.
    """
    wallet = load_wallet(config.wallet_key_path, network=config.network)
    try:
        raw = config.public_keys_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExecutorError(
            f"registri kunci publik tidak bisa dibaca ({config.public_keys_path}): {exc.strerror}"
        ) from None
    try:
        registry = parse_public_keys(raw)
    except EnvelopeError as exc:
        raise ExecutorError(f"registri kunci publik tidak sah: {exc}") from None

    return ExecutorRuntime(
        config=config,
        client=Web3Client(config.rpc_url) if client is None else client,
        wallet=wallet,
        registry=registry,
    )


def start_executor(
    config: ExecutorConfig,
    *,
    client: ChainClient | None = None,
    out: TextIO | None = None,
) -> tuple[ExecutorRuntime, StartupReport]:
    """Muat runtime lalu jalankan pemeriksaan start.

    Chain id yang salah dan ``decimals()`` yang bukan 6 menolak start; saldo
    gas yang kurang hanya dicetak sebagai peringatan.

    Raises:
        ChainError: bila chain id atau ``decimals()`` tidak cocok.
        ExecutorError: bila konfigurasi kunci publik tidak bisa dipakai.
    """
    stream = sys.stdout if out is None else out
    runtime = open_runtime(config, client=client)
    report = check_startup(
        runtime.client,
        expected_chain_id=config.chain_id,
        usdc_address=config.usdc_address,
        wallet_address=runtime.wallet.address,
        minimum_balance_wei=MINIMUM_GAS_BALANCE_WEI,
    )
    stream.write(
        f"Executor siap. chain_id {report.chain_id}, USDC {config.usdc_address} "
        f"decimals {report.decimals}, RPC {endpoint_host(config.rpc_url)}\n"
        f"wallet {report.wallet_address}\n"
    )
    for warning in report.warnings:
        stream.write(f"peringatan: {warning}\n")
    return runtime, report


# --------------------------------------------------------------------------- #
# Cek 1..5
# --------------------------------------------------------------------------- #


def verify_and_plan(
    envelope_json: str,
    *,
    cur: psycopg.Cursor,
    expected_chain_id: int,
    registry: Mapping[str, PublicKeyEntry],
) -> ExecutionPlan:
    """Jalankan cek 1 sampai 5. Hanya membaca; tidak ada efek samping.

    Tindakan database **pertama** fungsi ini adalah mengambil
    ``pg_advisory_xact_lock_shared(mandate:<agent_id>)``, sesuai aturan urutan
    lock di Arsitektur v1.1 bagian 4: lock ``mandate:`` sebelum tabel apa pun.
    Karena itu fungsi ini harus dipanggil sebagai langkah pertama di dalam
    transaksi pemanggil.

    Urutan cek: 1, 2, 3 (tanpa database), lalu 3b, 4, 5, dan 4b — sama dengan
    tabel di PRD v2.1 bagian 9.

    Cek 5 mendahului cek 4b dengan sengaja, dan itu soal apa yang dilaporkan,
    bukan apa yang lolos: himpunan envelope yang diterima sama untuk kedua
    urutan. Upaya mengeksekusi ulang keputusan yang sudah pernah dieksekusi
    adalah **sinyal replay**, dan ia harus tercatat sebagai
    ``already_executed``. Kalau 4b lebih dulu, cukup satu pergantian mandate
    sesudah eksekusi untuk membuat setiap upaya replay tercatat sebagai
    ``mandate_not_active`` — yaitu keluhan tentang aturan, bukan tentang
    pemakaian ulang kredensial. Yang kedua menyembunyikan yang pertama.
    Keputusan operator, 4 November 2026.

    Raises:
        CheckRejected: pada cek pertama yang gagal.
    """
    payload = parse_and_verify(
        envelope_json, registry=registry, expected_chain_id=expected_chain_id
    )

    # Cek 3b dan sesudahnya menyentuh database. Lock mandate lebih dulu.
    take_xact_lock_shared(cur, "mandate", payload.agent_id)
    now = db_now(cur)
    try:
        check_not_expired(payload, now)
    except VerificationError as exc:
        raise CheckRejected(REASON_EXPIRED, str(exc)) from None

    _require_identical_decision(cur, payload)
    _require_no_execution(cur, payload.decision_id)
    _require_active_mandate(cur, payload)

    return ExecutionPlan(
        decision_id=payload.decision_id,
        agent_id=payload.agent_id,
        recipient=payload.recipient,
        # Payload envelope menyimpan jumlah sebagai string digit; ini
        # batas sistem yang kedua, dan pintunya sama.
        amount_base=base_units(int(payload.amount_base)),
        mandate_hash=payload.mandate_hash,
        chain_id=payload.chain_id,
        valid_until=payload.valid_until,
        now=now,
    )


def parse_and_verify(
    envelope_json: str,
    *,
    registry: Mapping[str, PublicKeyEntry],
    expected_chain_id: int,
) -> Payload:
    """Cek 1, 2, dan 3. Tanpa database, tanpa jaringan, tanpa jam.

    Raises:
        CheckRejected: ``signature_invalid`` untuk bentuk dan tanda tangan,
            ``version_or_chain_mismatch`` atau ``not_approved`` untuk nilai.
    """
    try:
        envelope = parse_envelope(envelope_json)
    except EnvelopeError as exc:
        raise CheckRejected(REASON_SIGNATURE_INVALID, f"envelope ditolak parser: {exc}") from None

    try:
        payload = verify(envelope, registry)
    except (VerificationError, EnvelopeError) as exc:
        raise CheckRejected(_attribute_failure(envelope, expected_chain_id), str(exc)) from None

    # ``verify`` membandingkan ``chain_id`` dengan chain bawaannya sendiri
    # (:data:`payment_guard.chains.BASE_SEPOLIA`), bukan dengan nilai yang
    # dipegang Executor. Hari ini keduanya selalu sama, karena
    # ``load_executor_config`` menolak ``CHAIN_ID`` apa pun selain 84532.
    # Perbandingan di bawah tetap ada supaya ``expected_chain_id`` benar-benar
    # menjadi penjaga dan bukan sekadar bahan pesan error: kalau kelak kedua
    # sumber itu berbeda, yang menang harus konfigurasi Executor, dan
    # perbedaannya harus muncul sebagai penolakan, bukan sebagai kecocokan
    # yang kebetulan.
    if payload.chain_id != expected_chain_id:
        raise CheckRejected(
            REASON_VERSION_OR_CHAIN_MISMATCH,
            f"chain_id payload {payload.chain_id} bukan {expected_chain_id}",
        )
    return payload


def _attribute_failure(envelope: Envelope, expected_chain_id: int) -> str:
    """Tentukan apakah kegagalan :func:`verify` adalah cek 1 atau cek 2.

    :func:`payment_guard.signing.verify` memeriksa tanda tangan lebih dulu,
    lalu nilai, dan melempar satu jenis pengecualian untuk keduanya. Jadi bila
    salah satu nilai memang salah, kegagalannya cek 2; kalau semua nilai benar,
    yang gagal pasti tanda tangannya.
    """
    payload = envelope.payload
    if payload.status != SIGNED_STATUS:
        return REASON_NOT_APPROVED
    if payload.v != ENVELOPE_VERSION or payload.chain_id != expected_chain_id:
        return REASON_VERSION_OR_CHAIN_MISMATCH
    return REASON_SIGNATURE_INVALID


def _require_identical_decision(cur: psycopg.Cursor, payload: Payload) -> None:
    """Cek 4: baris ``decisions`` ada dan setiap field cocok dengan payload.

    Payload tidak dipercaya sendiri. Envelope yang sah tetap hanya pernyataan;
    yang mengikat adalah baris di database, dan keduanya harus sama.
    """
    cur.execute(SELECT_DECISION_FOR_EXECUTION, (payload.decision_id,))
    row = cur.fetchone()
    if row is None:
        raise CheckRejected(
            REASON_DECISION_MISMATCH,
            f"decision {payload.decision_id} tidak ada di database",
        )

    agent_id, recipient, amount_base, mandate_hash, chain_id, status, valid_until = row
    if status != Status.APPROVED.value:
        raise CheckRejected(
            REASON_NOT_APPROVED,
            f"status decision di database {status!r}, bukan {Status.APPROVED.value!r}",
        )

    mismatches = [
        name
        for name, seen, claimed in (
            ("agent_id", agent_id, payload.agent_id),
            ("recipient", recipient, payload.recipient),
            ("amount_base", to_int(amount_base), int(payload.amount_base)),
            ("mandate_hash", mandate_hash, payload.mandate_hash),
            ("chain_id", int(chain_id), payload.chain_id),
            ("valid_until", int(valid_until), payload.valid_until),
        )
        if seen != claimed
    ]
    if mismatches:
        raise CheckRejected(
            REASON_DECISION_MISMATCH,
            "payload envelope tidak cocok dengan baris decisions pada: " + ", ".join(mismatches),
        )


def _require_active_mandate(cur: psycopg.Cursor, payload: Payload) -> None:
    """Cek 4b: ``mandate_hash`` payload masih mandate aktif agent itu.

    Baris yang tidak ada **gagal**, bukan lolos: tanpa mandate aktif tidak ada
    aturan yang bisa dikatakan sedang berlaku.
    """
    cur.execute(SELECT_ACTIVE_MANDATE_HASH, (payload.agent_id,))
    row = cur.fetchone()
    if row is None:
        raise CheckRejected(
            REASON_MANDATE_NOT_ACTIVE,
            f"tidak ada mandate aktif untuk agent {payload.agent_id!r}",
        )
    if row[0] != payload.mandate_hash:
        raise CheckRejected(
            REASON_MANDATE_NOT_ACTIVE,
            "mandate aktif sudah berganti sejak persetujuan: envelope mengikat "
            f"{payload.mandate_hash}, yang aktif {row[0]}",
        )


def _require_no_execution(cur: psycopg.Cursor, decision_id: str) -> None:
    """Cek 5: belum ada baris ``executions`` untuk keputusan ini.

    ``UNIQUE(decision_id)`` yang menegakkannya; cek ini memberi pesan yang bisa
    dibaca sebelum database menolak dengan pelanggaran constraint.
    """
    cur.execute(SELECT_EXECUTION_STATUS, (decision_id,))
    row = cur.fetchone()
    if row is not None:
        raise CheckRejected(
            REASON_ALREADY_EXECUTED,
            f"decision ini sudah punya execution (status {row[0]!r}, tx {row[1]})",
        )


# --------------------------------------------------------------------------- #
# Cek 6: transaksi dibangun dari payload
# --------------------------------------------------------------------------- #


def build_transaction_plan(
    plan: ExecutionPlan,
    *,
    usdc_address: str,
    nonce: int,
    fees: Fees,
    gas: int,
) -> TransactionPlan:
    """Cek 6: susun transaksi **hanya** dari field yang sudah lolos cek.

    Dari ``plan`` diambil tepat tiga nilai: penerima, jumlah, dan ``chain_id``.
    Sisanya datang dari konfigurasi (alamat kontrak) atau dari RPC (nonce, gas,
    ongkos). Tidak ada argumen untuk sumber keempat, dan itu bukan kelalaian:
    satu-satunya cara menyisipkan tujuan atau jumlah lain adalah mengubah
    fungsi ini, yang akan terlihat di diff.
    """
    return TransactionPlan(
        to=usdc_address,
        data=encode_transfer(plan.recipient, plan.amount_base),
        value=TRANSFER_VALUE_WEI,
        chain_id=plan.chain_id,
        nonce=nonce,
        gas=gas,
        max_fee_per_gas=fees.max_fee_per_gas,
        max_priority_fee_per_gas=fees.max_priority_fee_per_gas,
    )


def next_nonce(cur: psycopg.Cursor, client: ChainClient, from_addr: str) -> int:
    """Nonce berikutnya untuk ``from_addr``: yang lebih besar dari dua sumber.

    Dua sumber, karena masing-masing bisa tertinggal:

    * ``eth_getTransactionCount(addr, "pending")`` adalah jawaban **node yang
      ini**. Node yang baru di-restart, atau node di belakang penyeimbang beban
      yang belum melihat transaksi kami, akan mengembalikan nonce yang sudah
      kami pakai. Mengandalkannya sendirian berarti menandatangani transaksi
      kedua dengan nonce yang sama, dan satu dari keduanya akan tergantikan
      tanpa jejak.
    * ``max(nonce)`` dari ``executions`` adalah catatan **kami**, dan ia
      di-commit sebelum broadcast. Ia tidak pernah tertinggal dari apa yang
      kami kirim, tapi ia tidak tahu transaksi lain dari wallet yang sama yang
      dikirim di luar sistem ini.

    Yang lebih besar menang, jadi tidak ada satu pun sumber yang bisa membuat
    nonce terpakai dua kali.

    **Rumus yang dikunci operator** (M6.0.1)::

        next_nonce = max(pending_rpc, coalesce(max_db_nonce, -1) + 1)

    Dua satuan yang berbeda disamakan dulu, dan di situlah seluruh isi rumus
    ini: ``max(nonce)`` adalah nonce **terakhir yang dipakai**, sedangkan
    ``pending`` sudah berupa nonce **berikutnya**. ``+ 1`` karena itu melekat
    pada sisi database saja, dan ``coalesce(..., -1)`` membuat tabel kosong
    menghasilkan 0 — bukan 1.

    Perbedaannya bukan selisih satu angka yang bisa diabaikan. Brief M6.0
    menuliskan rumusnya sebagai ``max(pending_rpc, max(nonce)) + 1``. Ditulis
    begitu, wallet baru tanpa riwayat mendapat nonce 1 pada transaksi
    pertamanya — karena ``pending`` sudah 0 untuk wallet kosong — sehingga
    nonce 0 tidak pernah dipakai dan **setiap** transaksi sesudahnya macet di
    belakang celah yang tidak akan pernah terisi. Wallet itu berhenti bisa
    membayar, dan tidak ada yang gagal sampai transaksi pertama tidak pernah
    ter-mine. Lihat ``docs/decisions.md``.

    ``UNIQUE(from_addr, nonce)`` tetap jaring pengaman terakhir: rumus ini
    mengurangi kesempatan tabrakan, tapi yang menolak tabrakan adalah database.

    Harus dipanggil di dalam lock ``executor:<from_addr>``: tanpa itu dua
    eksekusi paralel membaca nilai yang sama dan rumus apa pun tidak menolong.
    """
    cur.execute(SELECT_MAX_NONCE, (from_addr,))
    row = cur.fetchone()
    # Agregat selalu mengembalikan satu baris, dan `coalesce` di SQL sudah
    # mengubah tabel kosong menjadi -1. Keduanya tetap dijaga di sini karena
    # keliru di jalur ini menghasilkan nonce yang salah secara diam-diam, bukan
    # pengecualian.
    max_used = NO_NONCE_USED if row is None or row[0] is None else to_int(row[0])
    from_executions = max_used + 1
    from_rpc = client.pending_nonce(from_addr)
    return max(from_rpc, from_executions)


def estimate_gas_for(runtime: ExecutorRuntime, data: bytes) -> int:
    """Perkiraan gas untuk calldata ``data``, dari RPC.

    Alamat dikirim ber-checksum: node menolak alamat yang tidak ber-checksum,
    dan checksum dihitung di :mod:`payment_guard.addresses`.
    """
    return runtime.client.estimate_gas(
        {
            "from": to_checksum_address(runtime.wallet.address),
            "to": to_checksum_address(runtime.config.usdc_address),
            "data": "0x" + data.hex(),
            "value": TRANSFER_VALUE_WEI,
        }
    )


# --------------------------------------------------------------------------- #
# Eksekusi
# --------------------------------------------------------------------------- #


def execute_decision(
    runtime: ExecutorRuntime,
    decision_id: str,
    *,
    out: TextIO | None = None,
) -> ExecutionOutcome:
    """Eksekusi satu keputusan ``approved`` yang belum punya execution.

    Jalurnya: cek 1..6 dan write-ahead dalam satu transaksi, commit, lalu
    broadcast, lalu satu transaksi pendek untuk status ``broadcast``. Receipt
    **tidak** ditunggu di sini; :func:`recover_pending` yang menyelesaikannya.
    Itu membuat perintah ini tidak pernah menggantung, dan membuat penyelesaian
    punya satu jalur kode saja, yang sama untuk eksekusi normal dan untuk
    pemulihan setelah crash.

    Raises:
        ExecutorError: bila ``decision_id`` tidak sah atau barisnya tidak ada.
    """
    stream = sys.stdout if out is None else out
    require_uuid(decision_id)

    with connect(
        runtime.config.db_url,
        autocommit=True,
        lock_timeout_ms=runtime.config.lock_timeout_ms,
        statement_timeout_ms=runtime.config.lock_timeout_ms,
    ) as conn:
        try:
            # Dibaca di luar lock dan hanya untuk mendapatkan agent_id dari
            # envelope; yang mengikat adalah pembacaan ulang di dalam transaksi
            # (cek 4), yang berjalan di bawah lock mandate.
            #
            # Pembacaan ini ada DI DALAM try yang sama dengan jalur cek, dan
            # itu bukan kerapian: keputusan tanpa envelope adalah penolakan
            # cek 2 (``not_approved``), jadi ia harus ikut diaudit dan
            # dikembalikan sebagai hasil. Versi pertama menaruhnya di luar,
            # sehingga satu-satunya penolakan yang bisa terjadi sebelum
            # transaksi dibuka justru lolos tanpa tercatat.
            envelope_json = _read_envelope(conn, decision_id)
            signed, plan = _sign_write_ahead(runtime, conn, envelope_json)
        except CheckRejected as exc:
            _audit_rejection(conn, decision_id, exc.reason)
            stream.write(f"ditolak ({exc.reason}): {exc.detail}\n")
            return ExecutionOutcome(decision_id, OUTCOME_REJECTED, None, exc.detail)

        # Di titik ini fase 1 sudah commit dan SEMUA lock sudah dilepas.
        # Fase 2 (broadcast) dan fase 3 (catat status) berjalan tanpa lock:
        # perjalanan jaringan tidak boleh menahan eksekusi lain, dan nonce
        # kami sudah tercatat sehingga eksekusi berikutnya tidak akan
        # memakainya lagi walau node belum melihat transaksi ini.
        stream.write(
            f"ditandatangani: {signed.tx_hash}\n"
            f"  jumlah : {format_amount(plan.amount_base)} USDC\n"
            f"  tujuan : {plan.recipient}\n"
            f"  nonce  : {signed.nonce}\n"
        )
        return _broadcast(runtime, conn, plan, signed, stream)


def _sign_write_ahead(
    runtime: ExecutorRuntime, conn: psycopg.Connection, envelope_json: str
) -> tuple[SignedTransaction, ExecutionPlan]:
    """**Fase 1.** Satu transaksi: cek 1..6, tanda tangan lokal, INSERT, COMMIT.

    Seluruh lock hidup dan mati di dalam fungsi ini. Saat ia kembali, baris
    ``signed`` sudah di-commit dan lock ``mandate:`` serta ``executor:`` sudah
    dilepas — sehingga broadcast di fase 2 tidak menahan satu pun eksekusi lain
    selama perjalanan jaringan.
    """
    with conn.transaction(), conn.cursor() as cur:
        plan = verify_and_plan(
            envelope_json,
            cur=cur,
            expected_chain_id=runtime.config.chain_id,
            registry=runtime.registry,
        )

        # Nonce ditentukan SETELAH lock executor: satu penulis nonce per
        # wallet (Arsitektur v1.1 bagian 4). Tanpa lock ini, dua eksekusi
        # paralel membaca nonce yang sama dan salah satunya akan tergantikan.
        take_xact_lock(cur, "executor", runtime.wallet.address)

        data = encode_transfer(plan.recipient, plan.amount_base)
        transaction = build_transaction_plan(
            plan,
            usdc_address=runtime.config.usdc_address,
            nonce=next_nonce(cur, runtime.client, runtime.wallet.address),
            fees=runtime.client.fees(),
            gas=estimate_gas_for(runtime, data),
        )
        signed = runtime.wallet.sign(transaction.as_transaction())

        cur.execute(
            INSERT_EXECUTION,
            (
                plan.decision_id,
                signed.tx_hash,
                runtime.wallet.address,
                runtime.config.usdc_address,
                plan.amount_base,
                signed.nonce,
                signed.raw_tx,
                STATUS_SIGNED,
            ),
        )
        audit(
            cur,
            Event.EXECUTION_SIGNED,
            {
                "decision_id": plan.decision_id,
                "agent_id": plan.agent_id,
                "tx_hash": signed.tx_hash,
                "nonce": signed.nonce,
                "from_addr": runtime.wallet.address,
                "to_addr": runtime.config.usdc_address,
                "recipient": plan.recipient,
                "amount_base": format_base_units(plan.amount_base),
                "chain_id": plan.chain_id,
            },
        )
        return signed, plan


def _broadcast(
    runtime: ExecutorRuntime,
    conn: psycopg.Connection,
    plan: ExecutionPlan,
    signed: SignedTransaction,
    stream: TextIO,
) -> ExecutionOutcome:
    """Siarkan ``raw_tx`` lalu catat statusnya.

    Broadcast yang gagal **tidak** menandai apa pun ``failed``: barisnya tetap
    ``signed``, dan ``guard recover`` akan mencobanya lagi.
    """
    try:
        sent = runtime.client.send_raw_transaction(signed.raw_tx)
    except ChainError as exc:
        stream.write(
            f"broadcast gagal: {exc}\n"
            "Barisnya tetap 'signed'. Jalankan `guard recover` untuk mencoba lagi; "
            "byte yang sama disiarkan ulang, jadi tidak ada pembayaran kedua.\n"
        )
        return ExecutionOutcome(plan.decision_id, STATUS_SIGNED, signed.tx_hash, str(exc))

    with conn.transaction(), conn.cursor() as cur:
        _mark_broadcast(cur, plan.decision_id, sent, nonce=signed.nonce, agent_id=plan.agent_id)
    stream.write(
        f"disiarkan: {sent}\n"
        "Jalankan `guard recover` untuk mencatat receipt-nya setelah masuk blok.\n"
    )
    return ExecutionOutcome(plan.decision_id, STATUS_BROADCAST, sent, "disiarkan")


def _mark_broadcast(
    cur: psycopg.Cursor, decision_id: str, tx_hash: str, *, nonce: int, agent_id: str | None
) -> bool:
    """Tandai baris ``signed`` menjadi ``broadcast`` dan catat kejadiannya.

    ``WHERE status = 'signed'`` yang membuatnya aman dijalankan dua kali:
    penulis kedua tidak mengubah baris dan karena itu tidak menulis kejadian
    kedua. Mengembalikan True bila baris inilah yang berubah.
    """
    cur.execute(UPDATE_EXECUTION_BROADCAST, (STATUS_BROADCAST, decision_id, STATUS_SIGNED))
    if cur.rowcount == 0:
        return False
    payload: dict[str, object] = {
        "decision_id": decision_id,
        "tx_hash": tx_hash,
        "nonce": nonce,
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    audit(cur, Event.EXECUTION_BROADCAST, payload)
    return True


# --------------------------------------------------------------------------- #
# Recovery
# --------------------------------------------------------------------------- #


def fetch_pending(config: ExecutorConfig) -> list[PendingExecution]:
    """Baca semua baris ``executions`` yang masih ``signed`` atau ``broadcast``."""
    with (
        connect(
            config.db_url,
            autocommit=True,
            lock_timeout_ms=config.lock_timeout_ms,
            statement_timeout_ms=config.lock_timeout_ms,
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(SELECT_PENDING_EXECUTIONS, (list(PENDING_STATUSES),))
        return [_pending_from_row(row) for row in cur.fetchall()]


def recover_pending(
    runtime: ExecutorRuntime,
    *,
    out: TextIO | None = None,
) -> list[ExecutionOutcome]:
    """Selesaikan setiap ``signed`` dan ``broadcast`` yang tertinggal.

    Idempoten: menjalankannya dua kali memberi hasil yang sama, dan tidak
    pernah menghasilkan pembayaran kedua. Untuk baris ``signed`` yang belum
    pernah sampai ke jaringan, ``raw_tx`` **yang sama** disiarkan ulang, jadi
    ``tx_hash``-nya juga sama; node menolak duplikat, dan nonce yang sama tidak
    bisa dipakai dua transaksi berbeda.

    Aturan ``failed`` (PRD v2.1 bagian 9) ditegakkan di sini: hanya receipt
    yang revert, atau nonce yang sudah dipakai transaksi lain. Tidak ada jalur
    yang menandai ``failed`` karena lambat.
    """
    stream = sys.stdout if out is None else out
    outcomes: list[ExecutionOutcome] = []

    with connect(
        runtime.config.db_url,
        autocommit=True,
        lock_timeout_ms=runtime.config.lock_timeout_ms,
        statement_timeout_ms=runtime.config.lock_timeout_ms,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_PENDING_EXECUTIONS, (list(PENDING_STATUSES),))
            pending = [_pending_from_row(row) for row in cur.fetchall()]

        for item in pending:
            outcome = _recover_one(runtime, conn, item)
            outcomes.append(outcome)
            stream.write(f"{outcome.decision_id}  {outcome.status}  {outcome.detail}\n")

    if not outcomes:
        stream.write("Tidak ada execution yang tertunda.\n")
    return outcomes


def _recover_one(
    runtime: ExecutorRuntime, conn: psycopg.Connection, item: PendingExecution
) -> ExecutionOutcome:
    """Satu baris tertunda, seluruhnya di dalam lock ``executor:<from_addr>``.

    Lock-nya diambil sebelum barisnya dibaca ulang, dan dipegang sampai
    statusnya tertulis. Itu membuat dua ``guard recover`` yang berjalan
    bersamaan tidak bisa melihat keadaan chain yang berbeda lalu menuliskan dua
    kesimpulan yang berbeda untuk baris yang sama.

    Pembacaan ulang di dalam lock juga perlu: daftar tertunda dibaca di luar
    transaksi, jadi barisnya bisa sudah selesai sejak dibaca.
    """
    with conn.transaction(), conn.cursor() as cur:
        take_xact_lock(cur, "executor", item.from_addr)
        cur.execute(SELECT_EXECUTION_ROW, (item.decision_id,))
        row = cur.fetchone()
        if row is None:  # pragma: no cover - executions tidak pernah dihapus
            return ExecutionOutcome(item.decision_id, item.status, item.tx_hash, "baris hilang")
        current = _pending_from_row(row)
        if current.status not in PENDING_STATUSES:
            return ExecutionOutcome(
                current.decision_id, current.status, current.tx_hash, "sudah selesai sebelumnya"
            )

        receipt = runtime.client.receipt(current.tx_hash)
        if receipt is not None:
            return _settle_from_receipt(cur, current, receipt)

        # Belum ada receipt untuk hash KAMI. Satu-satunya hal yang membuat itu
        # permanen adalah nonce-nya sudah dipakai transaksi lain: kalau nonce
        # yang sudah dikonfirmasi melewati nonce kita, transaksi kita tidak
        # akan pernah bisa masuk.
        confirmed = runtime.client.confirmed_nonce(current.from_addr)
        if confirmed > current.nonce:
            return _mark_failed(
                cur,
                current,
                cause=CAUSE_NONCE_TAKEN,
                detail=(
                    f"nonce {current.nonce} sudah dilewati (nonce terkonfirmasi "
                    f"{confirmed}) dan tidak ada receipt untuk transaksi ini"
                ),
            )

        return _rebroadcast(runtime, cur, current)


def _settle_from_receipt(
    cur: psycopg.Cursor, item: PendingExecution, receipt: Receipt
) -> ExecutionOutcome:
    """Receipt sudah ada: transaksi memang terkirim, tinggal hasilnya.

    Baris yang masih ``signed`` ditandai ``broadcast`` lebih dulu. Itu bukan
    kerapian: CHECK ``broadcast_has_timestamp`` menuntut ``broadcast_at`` untuk
    status ``confirmed``, dan secara faktual transaksi ini memang sudah
    disiarkan — crash-nya terjadi setelah node menerimanya.
    """
    if item.status == STATUS_SIGNED:
        _mark_broadcast(cur, item.decision_id, item.tx_hash, nonce=item.nonce, agent_id=None)

    if receipt.reverted:
        return _mark_failed(
            cur,
            item,
            cause=CAUSE_RECEIPT_REVERTED,
            detail=f"receipt di blok {receipt.block_number} berstatus {receipt.status}",
            block_number=receipt.block_number,
        )

    cur.execute(
        UPDATE_EXECUTION_STATUS,
        (STATUS_CONFIRMED, item.decision_id, list(PENDING_STATUSES)),
    )
    if cur.rowcount == 0:  # pragma: no cover - dicegah lock dan pembacaan ulang
        return ExecutionOutcome(
            item.decision_id, STATUS_CONFIRMED, item.tx_hash, "sudah selesai sebelumnya"
        )
    audit(
        cur,
        Event.EXECUTION_CONFIRMED,
        {
            "decision_id": item.decision_id,
            "tx_hash": item.tx_hash,
            "nonce": item.nonce,
            "block_number": receipt.block_number,
        },
    )
    return ExecutionOutcome(
        item.decision_id, STATUS_CONFIRMED, item.tx_hash, f"blok {receipt.block_number}"
    )


def _mark_failed(
    cur: psycopg.Cursor,
    item: PendingExecution,
    *,
    cause: str,
    detail: str,
    block_number: int | None = None,
) -> ExecutionOutcome:
    """Tandai ``failed``. Hanya dua sebab yang sah (:data:`FAILURE_CAUSES`).

    Penjaga ``cause`` di sini bukan hiasan: ia membuat jalur "failed karena
    timeout" tidak bisa ditambahkan tanpa juga menambah namanya ke daftar
    tertutup, yang akan terlihat di diff.
    """
    if cause not in FAILURE_CAUSES:
        raise ExecutorError(f"sebab kegagalan tidak dikenal: {cause!r}")
    cur.execute(
        UPDATE_EXECUTION_STATUS,
        (STATUS_FAILED, item.decision_id, list(PENDING_STATUSES)),
    )
    if cur.rowcount == 0:  # pragma: no cover - dicegah lock dan pembacaan ulang
        return ExecutionOutcome(
            item.decision_id, STATUS_FAILED, item.tx_hash, "sudah selesai sebelumnya"
        )
    payload: dict[str, object] = {
        "decision_id": item.decision_id,
        "tx_hash": item.tx_hash,
        "nonce": item.nonce,
        "cause": cause,
    }
    if block_number is not None:
        payload["block_number"] = block_number
    audit(cur, Event.EXECUTION_FAILED, payload)
    return ExecutionOutcome(item.decision_id, STATUS_FAILED, item.tx_hash, detail)


def _rebroadcast(
    runtime: ExecutorRuntime, cur: psycopg.Cursor, item: PendingExecution
) -> ExecutionOutcome:
    """Siarkan ulang byte yang sama. Tidak pernah membangun transaksi baru.

    Membangun ulang akan memberi nonce atau ongkos yang berbeda, karena itu
    hash yang berbeda, karena itu dua pembayaran yang bisa dua-duanya masuk.
    Yang disiarkan di sini adalah ``raw_tx`` yang sudah dikomit.
    """
    try:
        sent = runtime.client.send_raw_transaction(item.raw_tx)
    except ChainError as exc:
        # Tetap tertunda. Lambat, tertolak sementara, atau node yang sedang
        # tidak bisa dihubungi bukan alasan menandai pembayaran gagal.
        return ExecutionOutcome(
            item.decision_id, item.status, item.tx_hash, f"masih tertunda: {exc}"
        )

    if item.status == STATUS_SIGNED:
        _mark_broadcast(cur, item.decision_id, sent, nonce=item.nonce, agent_id=None)
        return ExecutionOutcome(item.decision_id, STATUS_BROADCAST, sent, "disiarkan ulang")
    return ExecutionOutcome(
        item.decision_id, STATUS_BROADCAST, sent, "disiarkan ulang, menunggu receipt"
    )


# --------------------------------------------------------------------------- #
# Jalur perintah
# --------------------------------------------------------------------------- #


def run_execute(
    config: ExecutorConfig,
    decision_id: str,
    *,
    client: ChainClient | None = None,
    out: TextIO | None = None,
) -> int:
    """``guard execute``: pemeriksaan start, recovery, lalu satu eksekusi.

    Recovery berjalan lebih dulu dengan sengaja (PRD v2.1 bagian 9): execution
    yang tertinggal dari crash sebelumnya memakai nonce yang sudah dialokasikan,
    dan menambah transaksi baru di atasnya hanya memperbanyak yang harus
    dipulihkan.
    """
    stream = sys.stdout if out is None else out
    runtime, _ = start_executor(config, client=client, out=stream)
    recover_pending(runtime, out=stream)
    outcome = execute_decision(runtime, decision_id, out=stream)
    return 0 if outcome.status != OUTCOME_REJECTED else 1


def run_recover(
    config: ExecutorConfig,
    *,
    client: ChainClient | None = None,
    out: TextIO | None = None,
) -> int:
    """``guard recover``: selesaikan execution yang tertinggal. Idempoten."""
    stream = sys.stdout if out is None else out
    runtime, _ = start_executor(config, client=client, out=stream)
    recover_pending(runtime, out=stream)
    return 0


# --------------------------------------------------------------------------- #
# Pembantu
# --------------------------------------------------------------------------- #


def require_uuid(decision_id: str) -> None:
    """Pastikan ``decision_id`` UUID huruf kecil kanonik.

    Ada pemeriksa serupa di :mod:`payment_guard.approvals_cli`. Keduanya
    sengaja tidak saling mengimpor: Approvals berjalan sebagai role approver
    dan Executor sebagai role executor, dan satu-satunya alasan mereka berbagi
    kode akan membuat satu proses memuat modul milik proses lain beserta
    seluruh impornya.

    Raises:
        ExecutorError: bila bentuknya tidak sah.
    """
    if not isinstance(decision_id, str) or len(decision_id) != _UUID_LENGTH:
        raise ExecutorError("decision_id harus UUID")
    try:
        parsed = uuid.UUID(decision_id)
    except ValueError:
        raise ExecutorError("decision_id harus UUID") from None
    if str(parsed) != decision_id:
        raise ExecutorError("decision_id harus UUID huruf kecil kanonik")


def _read_envelope(conn: psycopg.Connection, decision_id: str) -> str:
    """Ambil ``envelope`` sebagai teks JSON dari baris ``decisions``.

    Raises:
        ExecutorError: bila barisnya tidak ada.
        CheckRejected: bila belum ada envelope, yaitu keputusan yang bukan
            ``approved``. Itu cek 2 (``not_approved``), bukan kesalahan
            pemanggil.
    """
    with conn.cursor() as cur:
        cur.execute(SELECT_ENVELOPE, (decision_id,))
        row = cur.fetchone()
    if row is None:
        raise ExecutorError(f"decision_id tidak ditemukan: {decision_id}")
    envelope_json, status = row
    if envelope_json is None:
        raise CheckRejected(
            REASON_NOT_APPROVED,
            f"keputusan ini berstatus {status!r} dan tidak punya envelope, "
            "jadi tidak ada persetujuan yang bisa dieksekusi",
        )
    return str(envelope_json)


def _audit_rejection(conn: psycopg.Connection, decision_id: str, reason: str) -> None:
    """Catat ``execution_rejected`` di transaksinya sendiri.

    Transaksi pemeriksaan sudah di-rollback ketika penolakan terjadi, jadi
    catatannya ditulis sesudahnya. Payload memuat alasan dari daftar tertutup
    dan **tidak** memuat detail yang bisa berisi nilai dari envelope.
    """
    with conn.transaction(), conn.cursor() as cur:
        audit(
            cur,
            Event.EXECUTION_REJECTED,
            {"decision_id": decision_id, "check": reason},
        )


def _require_bytes(value: object, label: str) -> bytes:
    """Baca kolom ``BYTEA`` sebagai ``bytes``.

    psycopg mengembalikan ``bytes`` untuk ``BYTEA``, tapi ``memoryview`` bukan
    hal yang mustahil tergantung adapter. Keduanya diterima; apa pun selain itu
    adalah baris yang tidak bisa dipakai, dan lebih baik gagal di sini daripada
    menyiarkan ulang sesuatu yang bukan transaksi.

    Menggantikan ``type: ignore`` yang sebelumnya menutupi pemeriksaan ini.

    Raises:
        ExecutorError: bila nilainya bukan byte.
    """
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray | memoryview):
        return bytes(value)
    raise ExecutorError(f"{label} harus bytes, bukan {type(value).__name__}")


def _pending_from_row(row: Sequence[object]) -> PendingExecution:
    return PendingExecution(
        decision_id=str(row[0]),
        tx_hash=str(row[1]),
        from_addr=str(row[2]),
        nonce=to_int(row[3]),
        raw_tx=_require_bytes(row[4], "raw_tx"),
        status=str(row[5]),
    )
