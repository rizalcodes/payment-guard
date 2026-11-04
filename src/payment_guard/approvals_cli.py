"""Approvals CLI: operator menyetujui, menolak, dan mengedaluwarsakan keputusan.

PRD v2 bagian 6.5, Arsitektur v1.1 bagian 7, Security v1.1 E3 dan ST-23.

Empat sifat yang dijaga modul ini, dan semuanya mudah hilang kalau tidak
ditulis sebagai syarat:

**Hanya manusia.** Tidak ada ``--yes``. Perintah menolak jalan bila ``stdin``
bukan TTY (ST-23). Mode non-interaktif berarti ada yang menjalankannya dari
skrip, dan skrip bukan operator.

**Konfirmasi anti-refleks.** Token enam karakter dibangkitkan acak, ditampilkan
sekali, dan harus diketik ulang. Ini bukan kontrol keamanan (Security v1.1
RR-9); ia hanya memastikan ada satu detik berpikir sebelum uang bergerak.

**Teks agent adalah data.** ``reason`` hanya muncul lewat
:func:`payment_guard.untrusted.render_untrusted`, tidak pernah mentah.

**Cek ulang di dalam lock.** ``needs_human`` tidak memesan jatah (PRD v2 bagian
6.3), jadi antara "agent meminta" dan "operator menyetujui" jatahnya bisa habis
dipakai pembayaran lain. Karena itu batas per transaksi dan harian dihitung
ulang dengan data terbaru, di dalam ``pg_advisory_xact_lock(agent:<agent_id>)``
yang sama dengan yang dipakai ``check_payment``.

Role dan kunci
--------------

Berjalan sebagai **approver**, bukan Guard: hanya ``approver_rw`` yang punya
UPDATE untuk ``approved_at``, ``valid_until``, dan ``envelope``
(``0002_roles.sql``). Kunci penanda tangan tetap kunci Guard, dibaca dari
``GUARD_SIGNING_KEY_PATH`` dan hanya pada jalur approve; ``reject`` tidak
pernah menyentuh bahan kunci.

Guard tidak perlu hidup
-----------------------

Approve tetap boleh saat Guard mati: keputusan hidup di database, bukan di
proses. Tapi keadaan Guard ikut dicatat (``guard_running``), karena "disetujui
saat Guard mati" dan "disetujui saat Guard jalan" adalah dua cerita berbeda
ketika sesuatu harus direkonstruksi nanti.
"""

from __future__ import annotations

import secrets
import sys
import uuid
from dataclasses import dataclass
from typing import Final, TextIO

import psycopg
from psycopg.types.json import Jsonb

from payment_guard.config import ApprovalsConfig
from payment_guard.db import (
    connect,
    db_now,
    release_session_lock,
    take_xact_lock,
    to_int,
    try_session_lock,
)
from payment_guard.decision_service import ValidatedRequest, build_snapshot
from payment_guard.events import Actor, Event, audit
from payment_guard.guard_service import load_signing_identity
from payment_guard.mandate import Mandate, load_normalized
from payment_guard.money import BaseUnits, base_units, format_amount
from payment_guard.reasons import ReasonCode, Status
from payment_guard.rules import Request, Snapshot, evaluate
from payment_guard.signing import build_payload, sign
from payment_guard.untrusted import render_untrusted

#: Alfabet token konfirmasi. Tanpa I, L, O, 0, dan 1: operator mengetik ulang
#: apa yang ia baca di layar, dan pasangan karakter yang mirip hanya melahirkan
#: pengetikan ulang, bukan ketelitian.
#:
#: Namanya ``CONFIRM_*``, bukan ``TOKEN_*``: ruff S105 membaca nama yang memuat
#: "token" dengan nilai string sebagai rahasia yang ditanam di kode. Temuan
#: palsu itu tidak dimatikan dengan ``noqa``, melainkan dihilangkan penyebabnya,
#: supaya aturannya tetap ketat untuk rahasia yang sungguhan.
#: Alfabet Crockford base32: sepuluh digit dan dua puluh dua huruf.
#:
#: ``I``, ``L``, ``O``, dan ``U`` tidak ada. Tiga yang pertama karena mudah
#: tertukar dengan ``1``, ``1``, dan ``0``; yang keempat karena Crockford
#: membuangnya supaya token acak tidak membentuk kata yang tidak diinginkan.
#:
#: ``0`` dan ``1`` **tetap ada**, dan itu bukan kelalaian. Crockford menangani
#: kebingungan itu di sisi pembacaan, bukan dengan memperkecil alfabet: lihat
#: :func:`normalize_confirmation`, yang memetakan ``O`` ke ``0`` serta ``I``
#: dan ``L`` ke ``1``. Hasilnya operator yang mengetik ``O`` tetap berhasil,
#: bukan ditolak karena salah lihat.
CONFIRM_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: Panjang token konfirmasi (Arsitektur v1.1 bagian 7).
CONFIRM_LENGTH: Final = 6

#: Pemetaan pembacaan Crockford, diterapkan pada masukan yang sudah huruf besar.
_CONFIRM_SUBSTITUTIONS: Final[dict[str, str]] = {"O": "0", "I": "1", "L": "1"}

#: ``guard_running`` ditulis ke audit sebagai string, bukan bool.
#:
#: :mod:`payment_guard.canonical` menolak ``bool`` di seluruh jalur audit supaya
#: bentuk kanoniknya tidak ambigu, dan ``events.require_safe_payload`` memakai
#: pemeriksa yang sama.
GUARD_RUNNING_TRUE: Final = "true"
GUARD_RUNNING_FALSE: Final = "false"

_UUID_LENGTH: Final = 36

# --------------------------------------------------------------------------- #
# SQL statik. Tidak ada bagian yang dirakit; semua nilai lewat parameter.
# --------------------------------------------------------------------------- #

#: Antrean persetujuan: ``needs_human`` milik agent yang mandate-nya aktif.
#:
#: JOIN ke ``active_mandates`` disengaja. Keputusan milik agent tanpa mandate
#: aktif tidak bisa dinilai ulang dengan jujur, jadi ia tidak ditawarkan untuk
#: disetujui.
SELECT_PENDING: Final = """
SELECT d.id::text, d.agent_id, d.recipient, d.amount_base, d.reason_text,
       extract(epoch from d.expires_at)::bigint
FROM decisions d
JOIN active_mandates a ON a.agent_id = d.agent_id
WHERE d.status = %s
ORDER BY d.created_at
"""

SELECT_DECISION: Final = """
SELECT d.agent_id, d.recipient, d.amount_base, d.reason_text, d.status,
       d.mandate_hash, d.chain_id,
       extract(epoch from d.expires_at)::bigint
FROM decisions d
WHERE d.id = %s
"""

SELECT_ACTIVE_MANDATE: Final = """
SELECT a.mandate_hash, m.content
FROM active_mandates a
JOIN mandates m ON m.mandate_hash = a.mandate_hash
WHERE a.agent_id = %s
"""

#: ``decided_at = expires_at`` dituntut CHECK ``decision_state_timestamps``.
#: Waktu deteksi yang sebenarnya hidup di audit, bukan di kolom ini.
UPDATE_EXPIRED: Final = """
UPDATE decisions
SET status = %s, reason_code = %s, decided_at = expires_at
WHERE id = %s AND status = %s
"""

UPDATE_DECIDED: Final = """
UPDATE decisions
SET status = %s, reason_code = %s,
    decided_at = timestamptz 'epoch' + %s * interval '1 second'
WHERE id = %s AND status = %s
"""

UPDATE_APPROVED: Final = """
UPDATE decisions
SET status = %s, reason_code = %s,
    decided_at  = timestamptz 'epoch' + %s * interval '1 second',
    approved_at = timestamptz 'epoch' + %s * interval '1 second',
    valid_until = timestamptz 'epoch' + %s * interval '1 second',
    envelope = %s
WHERE id = %s AND status = %s
"""


class ApprovalError(RuntimeError):
    """Perintah approvals gagal. Pesannya ditujukan ke operator.

    Tidak pernah memuat bahan kunci, URL database, atau token konfirmasi.
    """


@dataclass(frozen=True)
class PendingDecision:
    """Satu baris antrean persetujuan."""

    decision_id: str
    agent_id: str
    recipient: str
    amount_base: BaseUnits
    reason_text: str
    expires_at: int


@dataclass(frozen=True)
class DecisionRow:
    """Baris ``decisions`` yang sedang ditangani, apa adanya dari database."""

    agent_id: str
    recipient: str
    amount_base: BaseUnits
    reason_text: str
    status: str
    mandate_hash: str
    chain_id: int
    expires_at: int


@dataclass(frozen=True)
class Outcome:
    """Hasil satu perintah approve atau reject."""

    decision_id: str
    status: str
    reason_code: str
    guard_running: bool


@dataclass(frozen=True)
class _MandateContext:
    """Identitas agent dan mandate-nya, untuk :func:`build_snapshot`.

    Memenuhi protokol ``decision_service.MandateHolder`` tanpa menjadi sesi
    Guard: Approvals tidak memegang lock sesi dan tidak memuat mandate dari
    disk, tapi harus memakai definisi "jatah terpakai" yang sama persis.
    """

    agent_id: str
    mandate: Mandate


# --------------------------------------------------------------------------- #
# Interaksi operator
# --------------------------------------------------------------------------- #


def make_token() -> str:
    """Token konfirmasi enam karakter, dari sumber acak kriptografis.

    Dibangkitkan **setiap kali** prompt ditampilkan, dan tidak pernah disimpan:
    token yang tetap akan dihafal, dan hafalan adalah refleks — justru yang
    hendak dihindari.

    Token ini **bukan** turunan dari ``decision_id``. Arsitektur v1.1 bagian 7
    sempat menetapkan "enam karakter terakhir ``decision_id``"; itu diganti
    karena ``decision_id`` dikembalikan ke agent lewat jawaban
    ``check_payment``, sehingga agent bisa menghitung sendiri token yang akan
    diminta ke operator, lalu menyelipkannya ke ``reason`` sebagai bagian dari
    bujukan. Token acak menutup jalur itu seluruhnya. Keputusan operator,
    4 November 2026.

    Yang tetap jujur: ini pengaman **anti-refleks** terhadap operator yang
    menyetujui tanpa membaca, bukan kontrol keamanan terhadap agent. Agent yang
    bisa menjalankan perintah di mesin operator bisa membaca layarnya, dan
    token apa pun tidak menolong di sana (Security v1.1 asumsi 2, RR-1).
    """
    return "".join(secrets.choice(CONFIRM_ALPHABET) for _ in range(CONFIRM_LENGTH))


def normalize_confirmation(typed: str) -> str:
    """Baca token yang diketik operator, dengan aturan pembacaan Crockford.

    Huruf besar-kecil diabaikan, spasi di tepi dibuang, dan ``O`` dibaca ``0``
    sementara ``I`` dan ``L`` dibaca ``1``. Ketiga huruf itu tidak pernah
    muncul di token yang dibangkitkan, jadi memetakannya tidak menambah
    tabrakan: ia hanya memaafkan salah lihat.
    """
    tegak = typed.strip().upper()
    for salah, benar in _CONFIRM_SUBSTITUTIONS.items():
        tegak = tegak.replace(salah, benar)
    return tegak


def require_interactive(stdin: TextIO) -> None:
    """Tolak mode non-interaktif (ST-23, Security v1.1 E3).

    Raises:
        ApprovalError: bila ``stdin`` bukan terminal.
    """
    if not stdin.isatty():
        raise ApprovalError(
            "perintah ini hanya jalan di terminal interaktif. "
            "Tidak ada flag untuk melewatinya: persetujuan pembayaran harus "
            "diketik manusia, bukan dijalankan skrip."
        )


def confirm_with_token(stdin: TextIO, out: TextIO) -> None:
    """Minta operator mengetik ulang token yang ditampilkan sekali.

    Raises:
        ApprovalError: bila yang diketik tidak cocok. Tidak ada yang diubah.
    """
    token = make_token()
    out.write(f"\nKetik ulang token ini untuk melanjutkan: {token}\n")
    out.write("token> ")
    out.flush()
    typed = stdin.readline()
    if not typed or normalize_confirmation(typed) != token:
        raise ApprovalError("token konfirmasi tidak cocok; tidak ada yang diubah")


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #


def fetch_pending(config: ApprovalsConfig) -> list[PendingDecision]:
    """Baca antrean ``needs_human`` untuk agent yang mandate-nya aktif."""
    with (
        connect(
            config.db_url,
            autocommit=True,
            lock_timeout_ms=config.lock_timeout_ms,
            statement_timeout_ms=config.lock_timeout_ms,
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(SELECT_PENDING, (Status.NEEDS_HUMAN.value,))
        rows = cur.fetchall()
    return [
        PendingDecision(
            decision_id=row[0],
            agent_id=row[1],
            recipient=row[2],
            amount_base=base_units(to_int(row[3])),
            reason_text=row[4] or "",
            expires_at=int(row[5]),
        )
        for row in rows
    ]


def list_pending(config: ApprovalsConfig, *, out: TextIO | None = None) -> int:
    """Cetak antrean persetujuan. Mengembalikan kode keluar."""
    stream = sys.stdout if out is None else out
    pending = fetch_pending(config)
    if not pending:
        stream.write("Tidak ada keputusan yang menunggu persetujuan.\n")
        return 0
    for item in pending:
        stream.write(
            f"\n{item.decision_id}\n"
            f"  agent      : {item.agent_id}\n"
            f"  jumlah     : {format_amount(item.amount_base)} USDC\n"
            f"  penerima   : {item.recipient}\n"
            f"  kedaluwarsa: {item.expires_at} (epoch UTC)\n"
        )
        write_reason(stream, item.reason_text)
    stream.write(f"\n{len(pending)} menunggu persetujuan.\n")
    return 0


def write_reason(stream: TextIO, reason_text: str) -> None:
    """Tulis ``reason`` sebagai blok data, dengan indentasi yang tetap utuh."""
    for line in render_untrusted(reason_text).splitlines():
        stream.write(f"  {line}\n")


# --------------------------------------------------------------------------- #
# approve dan reject
# --------------------------------------------------------------------------- #


def approve(
    config: ApprovalsConfig,
    decision_id: str,
    *,
    stdin: TextIO | None = None,
    out: TextIO | None = None,
) -> Outcome:
    """Setujui satu ``needs_human`` setelah cek ulang batas.

    Raises:
        ApprovalError: non-interaktif, token salah, id tidak sah, keputusan
            tidak ada, atau sudah diputuskan. Kedaluwarsa, mandate berganti,
            dan cek ulang yang gagal **bukan** error: ketiganya hasil yang sah,
            dikembalikan sebagai :class:`Outcome` setelah barisnya diperbarui.
    """
    return _decide(config, decision_id, approving=True, stdin=stdin, out=out)


def reject(
    config: ApprovalsConfig,
    decision_id: str,
    *,
    stdin: TextIO | None = None,
    out: TextIO | None = None,
) -> Outcome:
    """Tolak satu ``needs_human``. Memicu cooldown untuk (agent, penerima)."""
    return _decide(config, decision_id, approving=False, stdin=stdin, out=out)


def _decide(
    config: ApprovalsConfig,
    decision_id: str,
    *,
    approving: bool,
    stdin: TextIO | None,
    out: TextIO | None,
) -> Outcome:
    reader = sys.stdin if stdin is None else stdin
    stream = sys.stdout if out is None else out

    require_interactive(reader)
    require_uuid(decision_id)

    row = _read_decision(config, decision_id)
    _require_still_pending(row)
    guard_running = probe_guard_running(config, row.agent_id)

    _write_summary(stream, decision_id, row, approving=approving, guard_running=guard_running)
    confirm_with_token(reader, stream)

    outcome = _commit_decision(
        config, decision_id, approving=approving, guard_running=guard_running
    )
    stream.write(f"\n{outcome.status} ({outcome.reason_code})\n")
    return outcome


def _require_still_pending(row: DecisionRow) -> None:
    """Tolak keputusan yang sudah diputus, sebelum ada ringkasan dan prompt.

    Pemeriksaan yang **mengikat** tetap berada di dalam lock di
    :func:`_commit_decision`, dan ia tidak boleh dihapus: antara pembacaan ini
    dan lock itu, Guard atau operator lain masih bisa memutuskan baris yang
    sama. Yang diperbaiki di sini hanya urutan bicara. Sebelumnya operator
    disuguhi ringkasan lengkap dan diminta mengetik token enam karakter, lalu
    baru diberi tahu bahwa keputusannya sudah diambil — dan karena token
    dibangkitkan sekali lalu dibuang, usaha itu hilang seluruhnya.

    Raises:
        ApprovalError: bila statusnya bukan ``needs_human``.
    """
    if row.status != Status.NEEDS_HUMAN.value:
        raise ApprovalError(
            f"keputusan ini sudah diputuskan (status {row.status!r}); tidak ada yang diubah"
        )


def probe_guard_running(config: ApprovalsConfig, agent_id: str) -> bool:
    """True bila sesi Guard untuk ``agent_id`` sedang memegang lock-nya.

    Memakai koneksi sendiri yang langsung ditutup, dan melepas lock bila sempat
    didapat. Probe tidak boleh meninggalkan apa pun: lock sesi hidup selama
    koneksinya hidup, dan lock yang tertinggal akan membuat Guard berikutnya
    gagal start dengan alasan yang salah (ST-30, ST-44).
    """
    with (
        connect(
            config.db_url,
            autocommit=True,
            lock_timeout_ms=config.lock_timeout_ms,
            statement_timeout_ms=config.lock_timeout_ms,
        ) as conn,
        conn.cursor() as cur,
    ):
        acquired = try_session_lock(cur, "guard", agent_id)
        if acquired:
            release_session_lock(cur, "guard", agent_id)
    return not acquired


def _commit_decision(
    config: ApprovalsConfig,
    decision_id: str,
    *,
    approving: bool,
    guard_running: bool,
) -> Outcome:
    """Satu transaksi di dalam lock agent. Semua pemeriksaan ada di sini.

    Yang dibaca sebelum konfirmasi hanya untuk ditampilkan; yang mengikat
    adalah pembacaan di dalam lock ini.
    """
    with connect(
        config.db_url,
        autocommit=True,
        lock_timeout_ms=config.lock_timeout_ms,
        statement_timeout_ms=config.lock_timeout_ms,
    ) as conn:
        with conn.transaction(), conn.cursor() as cur:
            row = _fetch_decision_row(cur, decision_id)
            take_xact_lock(cur, "agent", row.agent_id)
            # Dibaca ulang setelah lock: antara pembacaan pertama dan lock,
            # Guard atau operator lain bisa sudah memutuskan baris ini.
            row = _fetch_decision_row(cur, decision_id)
            if row.status != Status.NEEDS_HUMAN.value:
                raise ApprovalError(
                    f"keputusan ini sudah diputuskan (status {row.status!r}); tidak ada yang diubah"
                )

            now = db_now(cur)
            if now >= row.expires_at:
                return _expire(cur, decision_id, row, guard_running=guard_running)

            mandate_hash, mandate = _active_mandate(cur, row.agent_id)
            if mandate_hash != row.mandate_hash:
                return _refuse(
                    cur,
                    decision_id,
                    row,
                    status=Status.DENIED,
                    reason_code=ReasonCode.MANDATE_CHANGED,
                    now=now,
                    guard_running=guard_running,
                )

            if not approving:
                return _refuse(
                    cur,
                    decision_id,
                    row,
                    status=Status.REJECTED_BY_HUMAN,
                    reason_code=ReasonCode.HUMAN_REJECTED,
                    now=now,
                    guard_running=guard_running,
                )

            failure = _recheck(cur, row, mandate, now)
            if failure is not None:
                return _refuse(
                    cur,
                    decision_id,
                    row,
                    status=Status.DENIED,
                    reason_code=failure,
                    now=now,
                    guard_running=guard_running,
                )

            return _approve_row(
                cur,
                decision_id,
                row,
                mandate=mandate,
                now=now,
                guard_running=guard_running,
                config=config,
            )


def _recheck(
    cur: psycopg.Cursor, row: DecisionRow, mandate: Mandate, now: int
) -> ReasonCode | None:
    """Hitung ulang batas per transaksi dan harian. ``None`` berarti lolos.

    Yang dinilai ulang **hanya** dua batas itu (PRD v2 bagian 6.5, Arsitektur
    v1.1 bagian 7). Karena itu cooldown sengaja dinolkan sebelum memanggil
    :func:`payment_guard.rules.evaluate`: ``evaluate`` berhenti di aturan
    pertama yang cocok dan cooldown ada di urutan pertama, jadi membiarkannya
    terisi akan membuat satu penolakan yang tidak berhubungan menutupi
    pertanyaan yang sebenarnya diajukan di sini, yaitu "apakah jatahnya masih
    ada". Cooldown juga bukan alasan menolak operator: ia menahan agent yang
    mencoba berulang kali, bukan manusia yang menilai satu permintaan.

    Daftar penerima tidak ikut dinilai karena ``mandate_hash`` sudah dipastikan
    sama dengan mandate aktif sebelum fungsi ini dipanggil.
    """
    request = ValidatedRequest(
        recipient=row.recipient,
        amount_base=row.amount_base,
        chain=mandate.chain,
        chain_id=row.chain_id,
        reason="",
    )
    quota = build_snapshot(cur, _MandateContext(row.agent_id, mandate), request, now)
    decision = evaluate(
        Request(recipient=row.recipient, amount_base=row.amount_base, chain_id=row.chain_id),
        mandate,
        Snapshot(
            committed_day=quota.committed_day,
            committed_recipient=quota.committed_recipient,
            latest_triggering_denial=None,
        ),
        now,
    )
    if decision.reason_code is ReasonCode.OVER_PER_TX_LIMIT:
        return ReasonCode.RECHECK_OVER_PER_TX_LIMIT
    if decision.reason_code is ReasonCode.OVER_DAILY_LIMIT:
        return ReasonCode.RECHECK_OVER_DAILY_LIMIT
    return None


def _expire(
    cur: psycopg.Cursor, decision_id: str, row: DecisionRow, *, guard_running: bool
) -> Outcome:
    """Tandai kedaluwarsa. Kedaluwarsa lazy, Arsitektur v1.1 bagian 7."""
    cur.execute(
        UPDATE_EXPIRED,
        (
            Status.EXPIRED.value,
            ReasonCode.HUMAN_WAIT_TIMEOUT.value,
            decision_id,
            Status.NEEDS_HUMAN.value,
        ),
    )
    audit(
        cur,
        Event.DECISION_EXPIRED,
        _payload(decision_id, row, Status.EXPIRED, ReasonCode.HUMAN_WAIT_TIMEOUT, guard_running),
        actor=Actor.HUMAN,
    )
    return Outcome(
        decision_id=decision_id,
        status=Status.EXPIRED.value,
        reason_code=ReasonCode.HUMAN_WAIT_TIMEOUT.value,
        guard_running=guard_running,
    )


def _refuse(
    cur: psycopg.Cursor,
    decision_id: str,
    row: DecisionRow,
    *,
    status: Status,
    reason_code: ReasonCode,
    now: int,
    guard_running: bool,
) -> Outcome:
    """Tulis satu keputusan yang tidak menyetujui, lalu catat di audit."""
    cur.execute(
        UPDATE_DECIDED,
        (status.value, reason_code.value, now, decision_id, Status.NEEDS_HUMAN.value),
    )
    audit(
        cur,
        Event.APPROVAL_REJECTED,
        _payload(decision_id, row, status, reason_code, guard_running),
        actor=Actor.HUMAN,
    )
    return Outcome(
        decision_id=decision_id,
        status=status.value,
        reason_code=reason_code.value,
        guard_running=guard_running,
    )


def _approve_row(
    cur: psycopg.Cursor,
    decision_id: str,
    row: DecisionRow,
    *,
    mandate: Mandate,
    now: int,
    guard_running: bool,
    config: ApprovalsConfig,
) -> Outcome:
    """Setujui: tanda tangani envelope lalu simpan bersama stempel waktunya.

    Bahan kunci baru dibaca di sini, bukan saat perintah dimulai: jalur
    ``reject`` dan jalur yang berakhir kedaluwarsa tidak pernah membukanya.
    """
    identity = load_signing_identity(config.signing_key_path)
    valid_until = now + mandate.decision_ttl_seconds
    payload = build_payload(
        decision_id=decision_id,
        mandate_id=mandate.mandate_id,
        agent_id=row.agent_id,
        recipient=row.recipient,
        amount_base=row.amount_base,
        issued_at=now,
        valid_until=valid_until,
        mandate_hash=row.mandate_hash,
        chain=mandate.chain,
    )
    envelope = sign(payload, identity.signer, identity.key_id).as_dict()

    cur.execute(
        UPDATE_APPROVED,
        (
            Status.APPROVED.value,
            ReasonCode.APPROVED_BY_HUMAN.value,
            now,
            now,
            valid_until,
            Jsonb(envelope),
            decision_id,
            Status.NEEDS_HUMAN.value,
        ),
    )
    audit(
        cur,
        Event.APPROVAL_APPROVED,
        _payload(decision_id, row, Status.APPROVED, ReasonCode.APPROVED_BY_HUMAN, guard_running),
        actor=Actor.HUMAN,
    )
    return Outcome(
        decision_id=decision_id,
        status=Status.APPROVED.value,
        reason_code=ReasonCode.APPROVED_BY_HUMAN.value,
        guard_running=guard_running,
    )


def _payload(
    decision_id: str,
    row: DecisionRow,
    status: Status,
    reason_code: ReasonCode,
    guard_running: bool,
) -> dict[str, object]:
    """Payload audit. Tanpa ``reason``, tanpa envelope, tanpa token.

    ``reason_code`` boleh ikut: ia enum tertutup milik Guard, bukan teks agent.
    """
    return {
        "decision_id": decision_id,
        "agent_id": row.agent_id,
        "recipient": row.recipient,
        "amount_base": str(row.amount_base),
        "status": status.value,
        "reason_code": reason_code.value,
        "guard_running": GUARD_RUNNING_TRUE if guard_running else GUARD_RUNNING_FALSE,
    }


# --------------------------------------------------------------------------- #
# Pembacaan dan tampilan
# --------------------------------------------------------------------------- #


def require_uuid(decision_id: str) -> None:
    """Pastikan ``decision_id`` UUID huruf kecil kanonik.

    Raises:
        ApprovalError: bila bentuknya tidak sah.
    """
    if not isinstance(decision_id, str) or len(decision_id) != _UUID_LENGTH:
        raise ApprovalError("decision_id harus UUID")
    try:
        parsed = uuid.UUID(decision_id)
    except ValueError:
        raise ApprovalError("decision_id harus UUID") from None
    if str(parsed) != decision_id:
        raise ApprovalError("decision_id harus UUID huruf kecil kanonik")


def _read_decision(config: ApprovalsConfig, decision_id: str) -> DecisionRow:
    """Baca satu baris untuk ditampilkan, di luar lock."""
    with (
        connect(
            config.db_url,
            autocommit=True,
            lock_timeout_ms=config.lock_timeout_ms,
            statement_timeout_ms=config.lock_timeout_ms,
        ) as conn,
        conn.cursor() as cur,
    ):
        return _fetch_decision_row(cur, decision_id)


def _fetch_decision_row(cur: psycopg.Cursor, decision_id: str) -> DecisionRow:
    cur.execute(SELECT_DECISION, (decision_id,))
    row = cur.fetchone()
    if row is None:
        raise ApprovalError(f"decision_id tidak ditemukan: {decision_id}")
    if row[7] is None:
        raise ApprovalError(
            "keputusan ini tidak pernah menunggu manusia, jadi tidak ada yang "
            "bisa disetujui atau ditolak"
        )
    return DecisionRow(
        agent_id=row[0],
        recipient=row[1],
        amount_base=base_units(to_int(row[2])),
        reason_text=row[3] or "",
        status=row[4],
        mandate_hash=row[5],
        chain_id=int(row[6]),
        expires_at=int(row[7]),
    )


def _active_mandate(cur: psycopg.Cursor, agent_id: str) -> tuple[str, Mandate]:
    """Hash dan isi mandate yang sedang aktif untuk ``agent_id``."""
    cur.execute(SELECT_ACTIVE_MANDATE, (agent_id,))
    row = cur.fetchone()
    if row is None:
        raise ApprovalError(
            f"tidak ada mandate aktif untuk agent {agent_id!r}; "
            "jalankan Guard lebih dulu supaya aturannya jelas"
        )
    # Isi kolom ``content`` adalah bentuk ternormalisasi: jumlahnya base
    # units, bukan desimal USDC. ``load_mandate`` akan membacanya sejuta kali
    # lebih besar tanpa gagal sekali pun, dan setiap cek ulang batas lolos.
    return row[0], load_normalized(row[1])


def _write_summary(
    stream: TextIO,
    decision_id: str,
    row: DecisionRow,
    *,
    approving: bool,
    guard_running: bool,
) -> None:
    """Tampilkan detail lengkap sebelum konfirmasi (ST-23)."""
    tindakan = "MENYETUJUI" if approving else "MENOLAK"
    stream.write(
        f"\nAnda akan {tindakan} pembayaran berikut.\n\n"
        f"  decision_id: {decision_id}\n"
        f"  agent      : {row.agent_id}\n"
        f"  jumlah     : {format_amount(row.amount_base)} USDC\n"
        f"  penerima   : {row.recipient}\n"
        f"  chain_id   : {row.chain_id}\n"
        f"  kedaluwarsa: {row.expires_at} (epoch UTC)\n"
    )
    write_reason(stream, row.reason_text)
    if guard_running:
        stream.write(
            "\nPeringatan: Guard sedang jalan untuk agent ini. Keputusan Anda "
            "tetap sah; agent akan melihatnya lewat get_decision_status.\n"
        )
    else:
        stream.write(
            "\nPeringatan: Guard tidak berjalan untuk agent ini. Keputusan Anda "
            "tetap tersimpan, tapi agent baru akan melihatnya setelah Guard "
            "dijalankan lagi.\n"
        )
    if approving:
        stream.write("Batas per transaksi dan harian dicek ulang dengan data terbaru.\n")
