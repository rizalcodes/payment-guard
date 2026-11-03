"""Transaksi keputusan: satu-satunya tempat ``decisions`` ditulis.

PRD v2.1 bagian 6.4 (urutan evaluasi), PRD v2 bagian 6.3 (definisi jatah
terpakai), Arsitektur v1.1 bagian 4 (lock) dan bagian 6 (alur).

Satu panggilan ``check_payment`` adalah satu transaksi, dan urutannya tetap:

0. Validasi input. Gagal = **error input**, bukan keputusan: tidak ada baris
   ``decisions`` yang ditulis dan tidak ada cooldown yang terpicu.
1. ``pg_advisory_xact_lock(agent:<agent_id>)``. Semua pembacaan jatah dan
   penulisan keputusan untuk satu agent berjalan berurutan, yang membuat
   sepuluh permintaan paralel tidak bisa menembus batas harian (ST-13).
2. ``now`` dari **jam database**, satu kali, dipakai untuk seluruh keputusan.
3. Snapshot jatah dan penolakan pemicu terakhir, dihitung di SQL.
4. :func:`payment_guard.rules.evaluate` memutuskan. Fungsi murni; modul ini
   tidak pernah menilai aturan sendiri.
5. Satu baris ``decisions``, lalu audit ``decision_made``. Commit.

Dua hal yang sengaja tidak terjadi di sini
------------------------------------------

Field ``reason`` dari agent tidak pernah masuk ke :func:`evaluate`, tidak
pernah masuk payload audit, dan tidak pernah masuk envelope. Ia hanya mendarat
di kolom ``decisions.reason_text`` (PRD v2.1 bagian 3 nomor 10, Security v1.1
E1 dan T-8).

``agent_id`` tidak pernah datang dari permintaan. Ia selalu
``session.mandate.agent_id`` (Security v1.1 S1, ST-01).

Catatan waktu
-------------

Setiap timestamp yang ditulis diturunkan dari satu bilangan bulat ``now``,
termasuk ``created_at``. ``extract(epoch from now())::bigint`` membulatkan,
jadi ``created_at DEFAULT now()`` akan kadang lebih besar dari ``decided_at``
yang dihitung dari epoch itu, dan CHECK ``decided_not_before_created`` akan
gagal secara acak. Menurunkan semuanya dari satu bilangan bulat menghapus
seluruh kelas masalah itu.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

import psycopg
from psycopg.types.json import Jsonb

from payment_guard.addresses import AddressError, normalize_address
from payment_guard.chains import ChainError, chain_id_for
from payment_guard.db import connect, db_now, take_xact_lock, to_int
from payment_guard.events import Event, audit
from payment_guard.guard_service import GuardSession
from payment_guard.money import MoneyError, format_amount, format_base_units, parse_amount
from payment_guard.reasons import ReasonCode, Status, cooldown_triggering_pairs
from payment_guard.rules import Decision, Denial, Request, RuleInputError, Snapshot, evaluate
from payment_guard.signing import build_payload, sign

#: Batas panjang ``reason`` (PRD v2.1 bagian 3 nomor 10, ST-20). Karakter,
#: bukan byte, supaya cocok dengan CHECK ``char_length(reason_text) <= 500``.
MAX_REASON_LENGTH: Final = 500

#: Jendela jatah harian: 24 jam bergulir (PRD v2 bagian 6.3), bukan hari
#: kalender. Dihitung dari ``approved_at``.
DAY_SECONDS: Final = 86_400

#: Ambang ``repeated_retry``: lebih dari lima ``retry_blocked`` dalam satu
#: jendela cooldown (PRD v2 bagian 6.4).
REPEATED_RETRY_THRESHOLD: Final = 5

#: Status execution yang berarti jatahnya dilepas (PRD v2 bagian 6.3).
#:
#: Belum ada enum untuk status execution: tabelnya baru ditulis di M5, dan enum
#: yang nilainya belum pernah dipakai tidak bisa dibedakan dari enum yang
#: salah. Konstanta ini dikirim sebagai parameter, bukan ditanam di SQL.
EXECUTION_FAILED: Final = "failed"

# --------------------------------------------------------------------------- #
# SQL statik. Tidak ada bagian yang dirakit; semua nilai lewat parameter,
# termasuk nilai enum, supaya daftar status dan reason_code tetap satu sumber.
# --------------------------------------------------------------------------- #

#: Jatah terpakai, harian dan per recipient, dalam satu kali baca.
#:
#: "Terpakai" mengikuti PRD v2 bagian 6.3: ada execution yang tidak ``failed``,
#: ATAU belum ada execution dan ``now`` masih sebelum ``valid_until``. Cabang
#: pertama belum pernah benar sebelum M5 karena ``executions`` masih kosong,
#: tapi ia ditulis sekarang supaya definisinya tidak berubah saat M5 datang.
SELECT_COMMITTED: Final = """
SELECT
  coalesce(sum(d.amount_base) FILTER (
    WHERE d.approved_at >= timestamptz 'epoch' + %s * interval '1 second'
  ), 0) AS committed_day,
  coalesce(sum(d.amount_base) FILTER (
    WHERE d.recipient = %s
      AND d.approved_at >= timestamptz 'epoch' + %s * interval '1 second'
  ), 0) AS committed_recipient
FROM decisions d
WHERE d.agent_id = %s
  AND d.status = %s
  AND (
    EXISTS (
      SELECT 1 FROM executions e WHERE e.decision_id = d.id AND e.status <> %s
    )
    OR (
      NOT EXISTS (SELECT 1 FROM executions e WHERE e.decision_id = d.id)
      AND timestamptz 'epoch' + %s * interval '1 second' < d.valid_until
    )
  )
"""

#: Penolakan **pemicu cooldown** terakhir untuk (agent, recipient).
#:
#: Pasangan yang dianggap pemicu dikirim sebagai dua array sejajar, diturunkan
#: dari :func:`payment_guard.reasons.cooldown_triggering_pairs`. Daftarnya
#: tidak pernah ditulis ulang di SQL: kalau ditulis ulang, ia akan menyimpang
#: dari predikat yang dipakai ``rules``.
SELECT_LATEST_TRIGGERING_DENIAL: Final = """
SELECT extract(epoch from d.decided_at)::bigint, d.status, d.reason_code
FROM decisions d
WHERE d.agent_id = %s
  AND d.recipient = %s
  AND d.decided_at IS NOT NULL
  AND (d.status, d.reason_code) IN (SELECT * FROM unnest(%s::text[], %s::text[]))
ORDER BY d.decided_at DESC
LIMIT 1
"""

INSERT_DECISION: Final = """
INSERT INTO decisions (
  id, agent_id, mandate_hash, chain_id, recipient, amount_base, reason_text,
  status, reason_code,
  created_at, decided_at, approved_at, valid_until, expires_at, envelope
) VALUES (
  %s, %s, %s, %s, %s, %s, %s,
  %s, %s,
  timestamptz 'epoch' + %s * interval '1 second',
  timestamptz 'epoch' + %s * interval '1 second',
  timestamptz 'epoch' + %s * interval '1 second',
  timestamptz 'epoch' + %s * interval '1 second',
  timestamptz 'epoch' + %s * interval '1 second',
  %s
)
"""

COUNT_RETRY_BLOCKED_SINCE: Final = """
SELECT count(*)
FROM decisions
WHERE agent_id = %s
  AND recipient = %s
  AND status = %s
  AND reason_code = %s
  AND decided_at >= timestamptz 'epoch' + %s * interval '1 second'
"""

SELECT_DECISION_FOR_AGENT: Final = """
SELECT
  d.id::text, d.status, d.reason_code, d.recipient, d.amount_base, d.chain_id,
  d.mandate_hash, d.envelope,
  extract(epoch from d.expires_at)::bigint,
  extract(epoch from d.valid_until)::bigint
FROM decisions d
WHERE d.id = %s AND d.agent_id = %s
"""

#: Bentuk ``decision_id`` yang diterima. Sengaja ketat: UUID huruf kecil
#: kanonik, sama dengan yang ditulis modul ini dan dengan keluaran Postgres.
_UUID_LENGTH: Final = 36


class InputError(ValueError):
    """Masukan tool tidak sah.

    Error input (PRD v2.1 bagian 6.4 langkah 0), **bukan** keputusan: tidak ada
    baris ``decisions`` yang ditulis, dan tidak ada cooldown yang terpicu.
    """


@dataclass(frozen=True)
class CheckPaymentInput:
    """Empat argumen tool ``check_payment``, apa adanya dari agent."""

    recipient: str
    amount: str
    chain: str
    reason: str


@dataclass(frozen=True)
class ValidatedRequest:
    """Hasil langkah 0: nilai yang sudah dinormalisasi dan aman dipakai."""

    recipient: str
    amount_base: int
    chain: str
    chain_id: int
    reason: str


@dataclass(frozen=True)
class DecisionResult:
    """Keputusan yang sudah tersimpan, dalam bentuk jawaban tool.

    ``signed_decision`` hanya terisi untuk ``approved``; ``retry_after`` hanya
    untuk ``retry_blocked``; ``expires_at`` hanya untuk ``needs_human`` dan
    turunannya.
    """

    decision_id: str
    status: str
    reason_code: str
    agent_id: str
    recipient: str
    amount: str
    amount_base: str
    mandate_hash: str
    retry_after: int | None = None
    signed_decision: dict[str, object] | None = None
    expires_at: int | None = None

    def as_tool_response(self) -> dict[str, object]:
        """Bentuk respons tool. Field yang tidak berlaku dihilangkan, bukan di-null.

        PRD v2.1 bagian 7 menyebut ``retry_after`` "bila retry_blocked" dan
        ``signed_decision`` "bila approved"; menghilangkannya membuat agent
        tidak bisa salah membaca null sebagai nilai.
        """
        response: dict[str, object] = {
            "decision_id": self.decision_id,
            "status": self.status,
            "reason_code": self.reason_code,
            "agent_id": self.agent_id,
            "recipient": self.recipient,
            "amount": self.amount,
            "amount_base": self.amount_base,
            "mandate_hash": self.mandate_hash,
        }
        if self.retry_after is not None:
            response["retry_after"] = self.retry_after
        if self.signed_decision is not None:
            response["signed_decision"] = self.signed_decision
        if self.expires_at is not None:
            response["expires_at"] = self.expires_at
        return response


# --------------------------------------------------------------------------- #
# Langkah 0: validasi input
# --------------------------------------------------------------------------- #


def validate_input(raw: CheckPaymentInput, session: GuardSession) -> ValidatedRequest:
    """Jalankan langkah 0 PRD v2.1 bagian 6.4.

    Raises:
        InputError: pada pelanggaran pertama. Tidak ada efek samping.
    """
    if not isinstance(raw.reason, str):
        raise InputError(f"reason harus string, bukan {type(raw.reason).__name__}")
    if len(raw.reason) > MAX_REASON_LENGTH:
        raise InputError(f"reason maksimum {MAX_REASON_LENGTH} karakter, dapat {len(raw.reason)}")

    try:
        amount_base = parse_amount(raw.amount)
    except MoneyError as exc:
        raise InputError(f"amount: {exc}") from None

    try:
        recipient = normalize_address(raw.recipient)
    except AddressError as exc:
        raise InputError(f"recipient: {exc}") from None

    if not isinstance(raw.chain, str):
        raise InputError(f"chain harus string, bukan {type(raw.chain).__name__}")
    try:
        chain_id = chain_id_for(raw.chain)
    except ChainError as exc:
        raise InputError(f"chain: {exc}") from None
    if raw.chain != session.mandate.chain:
        raise InputError(
            f"chain {raw.chain!r} tidak sama dengan chain mandate ({session.mandate.chain!r})"
        )

    return ValidatedRequest(
        recipient=recipient,
        amount_base=amount_base,
        chain=raw.chain,
        chain_id=chain_id,
        reason=raw.reason,
    )


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #


def build_snapshot(
    cur: psycopg.Cursor, session: GuardSession, request: ValidatedRequest, now: int
) -> Snapshot:
    """Baca jatah terpakai dan penolakan pemicu terakhir, di dalam lock."""
    mandate = session.mandate
    cur.execute(
        SELECT_COMMITTED,
        (
            now - DAY_SECONDS,
            request.recipient,
            now - mandate.aggregation_window_seconds,
            session.agent_id,
            Status.APPROVED.value,
            EXECUTION_FAILED,
            now,
        ),
    )
    row = cur.fetchone()
    committed_day = to_int(row[0]) if row is not None else 0
    committed_recipient = to_int(row[1]) if row is not None else 0

    pairs = sorted(cooldown_triggering_pairs())
    cur.execute(
        SELECT_LATEST_TRIGGERING_DENIAL,
        (
            session.agent_id,
            request.recipient,
            [status.value for status, _ in pairs],
            [reason_code.value for _, reason_code in pairs],
        ),
    )
    denial_row = cur.fetchone()
    denial = None
    if denial_row is not None:
        denial = Denial(
            decided_at=int(denial_row[0]),
            status=Status(denial_row[1]),
            reason_code=ReasonCode(denial_row[2]),
        )

    return Snapshot(
        committed_day=committed_day,
        committed_recipient=committed_recipient,
        latest_triggering_denial=denial,
    )


# --------------------------------------------------------------------------- #
# check_payment
# --------------------------------------------------------------------------- #


def check_payment(session: GuardSession, raw: CheckPaymentInput) -> DecisionResult:
    """Nilai dan catat satu permintaan pembayaran.

    Raises:
        InputError: langkah 0 gagal; tidak ada yang ditulis.
        GuardUnavailableError: Guard sudah kehilangan lock sesinya.
    """
    session.require_healthy()
    request = validate_input(raw, session)

    # autocommit=True supaya blok ``transaction()`` di bawah selalu menjadi
    # BEGIN..COMMIT sungguhan. Pada koneksi non-autocommit, kueri apa pun
    # sebelum blok itu akan membuka transaksi implisit dan membuat blok ini
    # turun menjadi SAVEPOINT, yang berarti lock advisory transaksi dilepas
    # pada titik yang salah.
    with connect(
        session.config.db_url,
        autocommit=True,
        lock_timeout_ms=session.config.lock_timeout_ms,
        statement_timeout_ms=session.config.lock_timeout_ms,
    ) as conn:
        with conn.transaction(), conn.cursor() as cur:
            take_xact_lock(cur, "agent", session.agent_id)
            # Diperiksa ulang setelah lock didapat: heartbeat bisa gagal
            # sementara transaksi ini menunggu lock, dan Guard yang sudah
            # kehilangan lock tidak boleh menulis keputusan.
            session.require_healthy()

            now = db_now(cur)
            snapshot = build_snapshot(cur, session, request, now)

            try:
                decision = evaluate(
                    Request(
                        recipient=request.recipient,
                        amount_base=request.amount_base,
                        chain_id=request.chain_id,
                    ),
                    session.mandate,
                    snapshot,
                    now,
                )
            except RuleInputError as exc:  # pragma: no cover - berarti bug di sini
                raise RuntimeError(
                    f"snapshot atau request tidak sesuai kontrak rules: {exc}"
                ) from exc

            result = _persist(cur, session, request, decision, now)
            audit(
                cur,
                Event.DECISION_MADE,
                {
                    "decision_id": result.decision_id,
                    "status": result.status,
                    "reason_code": result.reason_code,
                    "agent_id": session.agent_id,
                    "recipient": request.recipient,
                    "amount_base": result.amount_base,
                },
            )
            if decision.reason_code is ReasonCode.RETRY_BLOCKED:
                _audit_repeated_retry(cur, session, request, snapshot, result)

    return result


def _persist(
    cur: psycopg.Cursor,
    session: GuardSession,
    request: ValidatedRequest,
    decision: Decision,
    now: int,
) -> DecisionResult:
    """Tulis satu baris ``decisions`` dan susun jawaban tool."""
    decision_id = str(uuid.uuid4())
    mandate = session.mandate

    decided_at: int | None = now
    approved_at: int | None = None
    valid_until: int | None = None
    expires_at: int | None = None
    envelope: dict[str, object] | None = None

    if decision.status is Status.APPROVED:
        approved_at = now
        valid_until = now + mandate.decision_ttl_seconds
        payload = build_payload(
            decision_id=decision_id,
            mandate_id=mandate.mandate_id,
            agent_id=mandate.agent_id,
            recipient=request.recipient,
            amount_base=request.amount_base,
            issued_at=now,
            valid_until=valid_until,
            mandate_hash=session.mandate_hash,
            chain=request.chain,
        )
        envelope = sign(payload, session.identity.signer, session.identity.key_id).as_dict()
    elif decision.status is Status.NEEDS_HUMAN:
        # needs_human tidak memesan jatah, dan belum punya decided_at.
        decided_at = None
        expires_at = now + mandate.human_wait_ttl_seconds

    cur.execute(
        INSERT_DECISION,
        (
            decision_id,
            mandate.agent_id,
            session.mandate_hash,
            request.chain_id,
            request.recipient,
            request.amount_base,
            request.reason,
            decision.status.value,
            decision.reason_code.value,
            now,
            decided_at,
            approved_at,
            valid_until,
            expires_at,
            Jsonb(envelope) if envelope is not None else None,
        ),
    )

    return DecisionResult(
        decision_id=decision_id,
        status=decision.status.value,
        reason_code=decision.reason_code.value,
        agent_id=mandate.agent_id,
        recipient=request.recipient,
        amount=format_amount(request.amount_base),
        amount_base=format_base_units(request.amount_base),
        mandate_hash=session.mandate_hash,
        retry_after=decision.retry_after,
        signed_decision=envelope,
        expires_at=expires_at,
    )


def _audit_repeated_retry(
    cur: psycopg.Cursor,
    session: GuardSession,
    request: ValidatedRequest,
    snapshot: Snapshot,
    result: DecisionResult,
) -> None:
    """Catat ``repeated_retry`` bila lebih dari lima retry dalam satu cooldown.

    Jendelanya dimulai dari penolakan pemicu, bukan dari percobaan terakhir:
    itulah arti "dalam satu cooldown" (PRD v2 bagian 6.4).
    """
    denial = snapshot.latest_triggering_denial
    if denial is None:  # pragma: no cover - retry_blocked selalu punya pemicu
        return
    cur.execute(
        COUNT_RETRY_BLOCKED_SINCE,
        (
            session.agent_id,
            request.recipient,
            Status.DENIED.value,
            ReasonCode.RETRY_BLOCKED.value,
            denial.decided_at,
        ),
    )
    row = cur.fetchone()
    attempts = to_int(row[0]) if row is not None else 0
    if attempts > REPEATED_RETRY_THRESHOLD:
        audit(
            cur,
            Event.REPEATED_RETRY,
            {
                "agent_id": session.agent_id,
                "recipient": request.recipient,
                "attempts": attempts,
                "cooldown_started_at": denial.decided_at,
                "decision_id": result.decision_id,
            },
        )


# --------------------------------------------------------------------------- #
# get_decision_status
# --------------------------------------------------------------------------- #


def get_decision_status(session: GuardSession, decision_id: str) -> DecisionResult | None:
    """Baca satu keputusan **milik agent ini**.

    Keputusan agent lain dijawab ``None``, yang pemanggil tampilkan sebagai
    "tidak ditemukan". Membedakan "tidak ada" dari "milik orang lain" akan
    memberi agent cara menghitung keputusan agent lain.

    Raises:
        InputError: bila ``decision_id`` bukan UUID.
        GuardUnavailableError: Guard sudah kehilangan lock sesinya.
    """
    session.require_healthy()
    if not isinstance(decision_id, str) or len(decision_id) != _UUID_LENGTH:
        raise InputError("decision_id harus UUID")
    try:
        parsed = uuid.UUID(decision_id)
    except ValueError:
        raise InputError("decision_id harus UUID") from None
    if str(parsed) != decision_id:
        raise InputError("decision_id harus UUID huruf kecil kanonik")

    with connect(
        session.config.db_url,
        autocommit=True,
        lock_timeout_ms=session.config.lock_timeout_ms,
        statement_timeout_ms=session.config.lock_timeout_ms,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_DECISION_FOR_AGENT, (decision_id, session.agent_id))
            row = cur.fetchone()

    if row is None:
        return None

    (
        stored_id,
        status,
        reason_code,
        recipient,
        amount_base,
        _chain_id,
        mandate_hash,
        envelope,
        expires_at,
        _valid_until,
    ) = row
    amount_int = to_int(amount_base)
    return DecisionResult(
        decision_id=stored_id,
        status=status,
        reason_code=reason_code,
        agent_id=session.agent_id,
        recipient=recipient,
        amount=format_amount(amount_int),
        amount_base=format_base_units(amount_int),
        mandate_hash=mandate_hash,
        retry_after=None,
        signed_decision=envelope,
        expires_at=int(expires_at) if expires_at is not None else None,
    )
