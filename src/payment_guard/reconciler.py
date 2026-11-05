"""Reconciler: cocokkan ``settlements`` dengan ``executions`` dan ``decisions``.

PRD v2 bagian 11, Arsitektur v1.1 bagian 11.

Reconciler **hanya mencatat**. Ia tidak pernah mengubah status ``decisions``
maupun ``executions``, dan itu bukan sekadar disiplin: ``0002_roles.sql``
hanya memberinya SELECT pada keduanya, jadi UPDATE dari modul ini akan ditolak
database. Satu-satunya tabel yang ditulisnya adalah ``reconciliations`` (plus
``audit_log``). Alasannya ada di Arsitektur v1.1 prinsip 2: proses yang
melaporkan ketidakcocokan tidak boleh juga punya kemampuan membuat
ketidakcocokan itu hilang.

Ia juga tidak menyentuh chain. Yang dibandingkan adalah tabel dengan tabel:
apa yang Watcher lihat di chain (``settlements``) terhadap apa yang kami
izinkan dan kirim (``decisions``, ``executions``). Bertanya ulang ke RPC di
sini akan berarti dua sumber kebenaran untuk hal yang sama.

**Tanpa lock.** Tabel lock di Arsitektur v1.1 bagian 4 adalah daftar tertutup,
dan ia tidak punya namespace untuk Reconciler — ``payment_guard.locks`` pun
menolak namespace di luar daftar itu. Dua Reconciler yang jalan bersamaan aman
karena ``UNIQUE (subject_type, subject_id)`` membuat kesimpulan yang sama
menimpa dirinya sendiri, bukan menghasilkan baris kedua. Yang bisa terjadi
hanyalah satu baris audit ganda, dan audit yang berlebih lebih baik daripada
lock yang tidak ada di dokumen.

Semua penilaian waktu memakai jam **database**.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, TextIO

import psycopg
from psycopg.types.json import Jsonb

from payment_guard.config import ReconcilerConfig
from payment_guard.db import connect, to_int
from payment_guard.events import Actor, Event, audit
from payment_guard.watcher import STATUS_FINAL, STATUS_REORGED

# --------------------------------------------------------------------------- #
# Keadaan rekonsiliasi
# --------------------------------------------------------------------------- #

#: Settlement ``final`` punya execution dengan tujuan dan jumlah yang sama.
STATE_MATCHED: Final = "matched"
#: Ada transfer dari wallet Executor yang tidak punya execution. **Alarm.**
#:
#: Artinya uang bergerak dari wallet kami lewat jalur di luar Payment Guard.
STATE_UNMATCHED_ONCHAIN: Final = "unmatched_onchain"
#: Execution-nya ada, tapi tujuan atau jumlahnya berbeda. **Alarm.**
STATE_MISMATCH: Final = "mismatch"
#: Sudah disiarkan, belum ada settlement ``final``, dan batas waktunya lewat.
STATE_EXECUTED_NOT_SETTLED: Final = "executed_not_settled"
#: Keputusan ``approved`` yang ``valid_until``-nya lewat tanpa execution.
STATE_EXPIRED: Final = "expired"
#: Settlement yang pernah ``final`` lalu ter-reorg. Barisnya tidak dihapus.
STATE_INVALIDATED: Final = "invalidated"

#: Daftar tertutup, sama dengan CHECK ``reconciliations.state`` di
#: ``0001_init.sql``. State ada di payload ``reconciliation_state_changed``,
#: bukan di nama kejadiannya.
RECONCILIATION_STATES: Final[tuple[str, ...]] = (
    STATE_MATCHED,
    STATE_UNMATCHED_ONCHAIN,
    STATE_MISMATCH,
    STATE_EXECUTED_NOT_SETTLED,
    STATE_EXPIRED,
    STATE_INVALIDATED,
)

#: State yang perlu perhatian operator. Dipakai kode keluar ``guard reconcile``.
ALARM_STATES: Final[frozenset[str]] = frozenset(
    {
        STATE_UNMATCHED_ONCHAIN,
        STATE_MISMATCH,
        STATE_EXECUTED_NOT_SETTLED,
        STATE_INVALIDATED,
    }
)

#: Jenis subjek, sama dengan CHECK ``reconciliations.subject_type``.
SUBJECT_SETTLEMENT: Final = "settlement"
SUBJECT_EXECUTION: Final = "execution"
SUBJECT_DECISION: Final = "decision"

SUBJECT_TYPES: Final[tuple[str, ...]] = (
    SUBJECT_SETTLEMENT,
    SUBJECT_EXECUTION,
    SUBJECT_DECISION,
)

#: Status execution yang berarti "sudah ada sesuatu di jaringan".
#:
#: ``signed`` tidak termasuk: baris itu ada sebelum ada apa pun yang terkirim
#: (write-ahead, PRD v2 bagian 9), jadi tidak adanya settlement untuknya bukan
#: temuan. ``failed`` juga tidak: ia sudah dinyatakan gagal oleh Executor.
BROADCAST_STATUSES: Final[tuple[str, ...]] = ("broadcast", "confirmed")


# --------------------------------------------------------------------------- #
# SQL. Statik; nilai selalu lewat parameter.
# --------------------------------------------------------------------------- #

#: Settlement yang perlu disimpulkan, beserta execution yang hash-nya sama.
#:
#: ``LEFT JOIN`` dengan sengaja: settlement **tanpa** execution adalah temuan
#: yang paling penting di modul ini, jadi ia harus ikut terbawa, bukan hilang
#: karena join-nya dalam. ``executions`` tidak punya ``chain_id``; yang
#: punya adalah ``decisions``, dan hash transaksi sudah unik di tabel itu.
#:
#: **Penerima dibandingkan dengan ``decisions.recipient``, bukan dengan
#: ``executions.to_addr``.** Keduanya terdengar seperti hal yang sama dan
#: bukan: pada transfer ERC-20, tujuan **transaksinya** adalah kontrak token,
#: dan penerima pembayaran ada di dalam calldata. ``executions.to_addr``
#: karena itu selalu berisi alamat kontrak USDC (lihat ``INSERT_EXECUTION`` di
#: :mod:`payment_guard.executor`), sedangkan ``settlements.to_addr`` berisi
#: ``to`` dari log ``Transfer``, yaitu penerima yang sesungguhnya.
#: Membandingkan kedua kolom itu akan membuat **setiap** pembayaran yang benar
#: dilaporkan ``mismatch`` — dan alarm yang selalu berbunyi sama dengan tidak
#: ada alarm. PRD v2 bagian 11 menulis "bandingkan ``to_addr``"; yang
#: dimaksudnya adalah penerima, dan penerima hidup di ``decisions``.
#:
#: ``d`` dijoin lewat ``e``: kalau ``e`` tidak ada, ``d`` juga tidak, dan
#: kesimpulannya ``unmatched_onchain`` tanpa perlu membandingkan apa pun.
#: ``finalized_at`` ikut dibaca karena ia satu-satunya cara membedakan dua
#: baris ``reorged`` yang terlihat sama: yang pernah ``final`` (kolomnya
#: terisi, dan Watcher tidak pernah mengosongkannya) dan yang hanya sempat
#: ``pending``. Hanya yang pertama menjadi ``invalidated`` — PRD v2 bagian 11
#: menulis "settlement **final** yang kemudian reorged". Yang kedua belum
#: pernah dihitung sebagai pembayaran, jadi tidak ada yang perlu dibatalkan.
SELECT_SETTLEMENTS: Final = """
SELECT s.id, s.tx_hash, s.log_index, s.to_addr, s.amount_base, s.status,
       e.id, d.recipient, e.amount_base, e.decision_id::text, s.finalized_at
FROM settlements s
LEFT JOIN executions e ON e.tx_hash = s.tx_hash
LEFT JOIN decisions d ON d.id = e.decision_id
WHERE s.chain_id = %s AND s.status = ANY(%s)
ORDER BY s.id
"""

#: Berapa banyak settlement ``final`` yang menunjuk satu hash transaksi.
#:
#: Lebih dari satu berarti satu transaksi memuat lebih dari satu Transfer dari
#: wallet Executor. PRD v2 bagian 11 menuntut "tepat satu log Transfer yang
#: cocok", dan ini yang memeriksanya.
SELECT_FINAL_LOG_COUNTS: Final = """
SELECT tx_hash, count(*) FROM settlements
WHERE chain_id = %s AND status = %s
GROUP BY tx_hash HAVING count(*) > 1
"""

#: Execution yang sudah disiarkan tapi belum punya settlement ``final``,
#: dan sudah melewati batas waktu menurut jam **database**.
#:
#: ``make_interval(secs => %s)`` dipakai supaya batas waktunya tetap parameter.
#: Merakit ``INTERVAL '3600 seconds'`` dari angka akan berarti SQL yang
#: disusun dari string, yang dilarang di repo ini.
SELECT_UNSETTLED_EXECUTIONS: Final = """
SELECT e.id, e.tx_hash, e.decision_id::text, e.status
FROM executions e
WHERE e.status = ANY(%s)
  AND coalesce(e.broadcast_at, e.created_at) < now() - make_interval(secs => %s)
  AND NOT EXISTS (
    SELECT 1 FROM settlements s
    WHERE s.tx_hash = e.tx_hash AND s.chain_id = %s AND s.status = %s
  )
ORDER BY e.id
"""

#: Keputusan ``approved`` yang kedaluwarsa tanpa pernah dieksekusi.
SELECT_EXPIRED_DECISIONS: Final = """
SELECT d.id::text, d.agent_id
FROM decisions d
WHERE d.status = %s
  AND d.chain_id = %s
  AND d.valid_until < now()
  AND NOT EXISTS (SELECT 1 FROM executions e WHERE e.decision_id = d.id)
ORDER BY d.id
"""

SELECT_EXISTING: Final = """
SELECT state FROM reconciliations WHERE subject_type = %s AND subject_id = %s
"""

INSERT_RECONCILIATION: Final = """
INSERT INTO reconciliations (
  subject_type, subject_id, state, decision_id, execution_id, settlement_id, detail
) VALUES (%s, %s, %s, %s, %s, %s, %s)
"""

#: Hanya tiga kolom yang di-UPDATE, dan hanya tiga itu yang di-GRANT
#: (``0002_roles.sql``). Tautan ke subjeknya tidak pernah berubah: kalau ia
#: berubah, itu subjek yang lain.
UPDATE_RECONCILIATION: Final = """
UPDATE reconciliations
SET state = %s, detail = %s, updated_at = now()
WHERE subject_type = %s AND subject_id = %s
"""


class ReconcilerError(RuntimeError):
    """Kegagalan di jalur Reconciler. Pesannya tidak memuat URL database."""


@dataclass(frozen=True)
class Finding:
    """Satu kesimpulan tentang satu subjek."""

    subject_type: str
    subject_id: str
    state: str
    detail: dict[str, object]
    decision_id: str | None = None
    execution_id: int | None = None
    settlement_id: int | None = None

    def __post_init__(self) -> None:
        if self.state not in RECONCILIATION_STATES:
            raise ReconcilerError(f"state rekonsiliasi tidak dikenal: {self.state!r}")
        if self.subject_type not in SUBJECT_TYPES:
            raise ReconcilerError(f"jenis subjek tidak dikenal: {self.subject_type!r}")

    @property
    def is_alarm(self) -> bool:
        return self.state in ALARM_STATES


@dataclass
class ReconcileReport:
    """Hasil satu siklus Reconciler."""

    findings: list[Finding] = field(default_factory=list)
    changed: int = 0

    @property
    def alarms(self) -> list[Finding]:
        return [f for f in self.findings if f.is_alarm]

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(RECONCILIATION_STATES, 0)
        for finding in self.findings:
            counts[finding.state] += 1
        return counts

    def render(self) -> str:
        """Laporan CLI: hitungan per state, lalu setiap alarm satu baris.

        Alarm disebut satu per satu dengan sengaja. Angka "3 mismatch" tidak
        bisa ditindaklanjuti; ``tx_hash``-nya bisa dibuka di explorer.
        """
        counts = self.counts()
        lines = [f"{sum(counts.values())} subjek diperiksa, {self.changed} berubah"]
        for state in RECONCILIATION_STATES:
            if counts[state]:
                lines.append(f"  {state}: {counts[state]}")
        alarms = self.alarms
        if not alarms:
            lines.append("tidak ada alarm")
            return "\n".join(lines)
        lines.append(f"ALARM ({len(alarms)}):")
        for finding in alarms:
            lines.append(f"  {finding.state} {finding.subject_type} {finding.subject_id}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Satu siklus
# --------------------------------------------------------------------------- #


def reconcile_once(
    config: ReconcilerConfig,
    *,
    out: TextIO | None = None,
) -> ReconcileReport:
    """Jalankan satu siklus rekonsiliasi dan tulis hasilnya.

    Tiga arah, semuanya dari PRD v2 bagian 11:

    1. Dari chain ke keputusan: setiap settlement ``final`` dicocokkan ke
       execution lewat ``tx_hash``.
    2. Dari keputusan ke chain: execution yang sudah disiarkan tapi belum
       punya settlement ``final`` setelah batas waktu.
    3. Keputusan ``approved`` yang kedaluwarsa tanpa pernah dieksekusi.

    Raises:
        ReconcilerError: bila keadaan database tidak masuk akal.
    """
    with connect(config.db_url, lock_timeout_ms=config.lock_timeout_ms) as conn:
        findings = collect_findings(conn, config)
        report = ReconcileReport(findings=list(findings))
        for finding in findings:
            if record(conn, finding):
                report.changed += 1
    if out is not None:
        print(report.render(), file=out)
    return report


def collect_findings(conn: psycopg.Connection, config: ReconcilerConfig) -> list[Finding]:
    """Semua kesimpulan siklus ini, tanpa menulis apa pun.

    Dipisah dari :func:`record` supaya pengambilan kesimpulan bisa diuji tanpa
    database yang bisa ditulis, dan supaya terbaca bahwa pembacaan terjadi
    sebelum penulisan — bukan berselang-seling dengannya.
    """
    findings: list[Finding] = []
    with conn.cursor() as cur:
        duplicates = _duplicate_final_logs(cur, config)
        findings.extend(_settlement_findings(cur, config, duplicates=duplicates))
        findings.extend(_execution_findings(cur, config))
        findings.extend(_decision_findings(cur, config))
    conn.rollback()
    return findings


def _duplicate_final_logs(cur: psycopg.Cursor, config: ReconcilerConfig) -> dict[str, int]:
    cur.execute(SELECT_FINAL_LOG_COUNTS, (config.chain_id, STATUS_FINAL))
    return {str(tx_hash): to_int(count) for tx_hash, count in cur.fetchall()}


def _settlement_findings(
    cur: psycopg.Cursor,
    config: ReconcilerConfig,
    *,
    duplicates: dict[str, int],
) -> list[Finding]:
    """Arah pertama: dari apa yang terjadi di chain ke apa yang kami izinkan."""
    cur.execute(SELECT_SETTLEMENTS, (config.chain_id, [STATUS_FINAL, STATUS_REORGED]))
    rows = cur.fetchall()
    findings: list[Finding] = []
    for row in rows:
        settlement_id = to_int(row[0])
        tx_hash = str(row[1])
        log_index = to_int(row[2])
        settled_to = str(row[3])
        settled_amount = to_int(row[4])
        settlement_status = str(row[5])
        execution_id = None if row[6] is None else to_int(row[6])
        detail: dict[str, object] = {
            "tx_hash": tx_hash,
            "log_index": log_index,
            "settlement_status": settlement_status,
        }

        if settlement_status == STATUS_REORGED:
            if row[10] is None:
                # Pernah `pending`, belum pernah `final`. Ia tidak pernah
                # dihitung sebagai pembayaran, jadi tidak ada kesimpulan yang
                # perlu dibatalkan — dan menamainya `invalidated` akan
                # menyamakannya dengan kejadian yang jauh lebih serius.
                continue
            # PRD v2 bagian 11: settlement yang pernah final lalu reorged
            # menjadi `invalidated`, dan barisnya tidak dihapus. Yang pernah
            # dihitung sebagai pembayaran harus terlihat pernah dihitung.
            findings.append(
                Finding(
                    subject_type=SUBJECT_SETTLEMENT,
                    subject_id=str(settlement_id),
                    state=STATE_INVALIDATED,
                    detail=detail,
                    settlement_id=settlement_id,
                    execution_id=execution_id,
                    decision_id=None if row[9] is None else str(row[9]),
                )
            )
            continue

        if execution_id is None:
            findings.append(
                Finding(
                    subject_type=SUBJECT_SETTLEMENT,
                    subject_id=str(settlement_id),
                    state=STATE_UNMATCHED_ONCHAIN,
                    detail=detail,
                    settlement_id=settlement_id,
                )
            )
            continue

        if row[7] is None:
            # `executions.decision_id` adalah NOT NULL dengan FK ke
            # `decisions`, jadi execution tanpa keputusan tidak bisa ada.
            # Kalau ia ada, yang salah bukan pembayarannya — melainkan
            # database — dan menebak di sini akan menyembunyikannya.
            raise ReconcilerError(
                f"execution {execution_id} tidak punya keputusan; "
                "FK executions.decision_id dilanggar"
            )
        expected_to = str(row[7])
        expected_amount = to_int(row[8])
        decision_id = None if row[9] is None else str(row[9])
        differences = []
        if settled_to != expected_to:
            differences.append("to_addr")
        if settled_amount != expected_amount:
            differences.append("amount_base")
        if duplicates.get(tx_hash, 1) > 1:
            differences.append("log_count")

        if differences:
            detail["differs"] = differences
            detail["expected_to_addr"] = expected_to
            # Jumlah sebagai string: `audit_log` tidak boleh memuat nilai yang
            # lewat float, dan base units bisa melampaui 2^53.
            detail["expected_amount_base"] = str(expected_amount)
            detail["settled_amount_base"] = str(settled_amount)
            if duplicates.get(tx_hash, 1) > 1:
                detail["final_logs_for_tx"] = duplicates[tx_hash]
            state = STATE_MISMATCH
        else:
            state = STATE_MATCHED

        findings.append(
            Finding(
                subject_type=SUBJECT_SETTLEMENT,
                subject_id=str(settlement_id),
                state=state,
                detail=detail,
                settlement_id=settlement_id,
                execution_id=execution_id,
                decision_id=decision_id,
            )
        )
    return findings


def _execution_findings(cur: psycopg.Cursor, config: ReconcilerConfig) -> list[Finding]:
    """Arah kedua: sudah kami kirim, tapi chain tidak pernah menunjukkannya."""
    cur.execute(
        SELECT_UNSETTLED_EXECUTIONS,
        (list(BROADCAST_STATUSES), config.deadline_seconds, config.chain_id, STATUS_FINAL),
    )
    findings = []
    for row in cur.fetchall():
        execution_id = to_int(row[0])
        findings.append(
            Finding(
                subject_type=SUBJECT_EXECUTION,
                subject_id=str(execution_id),
                state=STATE_EXECUTED_NOT_SETTLED,
                detail={
                    "tx_hash": str(row[1]),
                    "execution_status": str(row[3]),
                    "deadline_seconds": config.deadline_seconds,
                },
                execution_id=execution_id,
                decision_id=str(row[2]),
            )
        )
    return findings


def _decision_findings(cur: psycopg.Cursor, config: ReconcilerConfig) -> list[Finding]:
    """Arah ketiga: disetujui, jatahnya dipakai, tapi tidak pernah dieksekusi.

    Bukan alarm: jatah yang dilepas karena kedaluwarsa adalah jalur normal
    (PRD v2 bagian 11). Yang dicatat adalah bahwa keputusan itu selesai tanpa
    pembayaran, supaya laporan tidak meninggalkan baris menggantung.
    """
    cur.execute(SELECT_EXPIRED_DECISIONS, ("approved", config.chain_id))
    return [
        Finding(
            subject_type=SUBJECT_DECISION,
            subject_id=str(row[0]),
            state=STATE_EXPIRED,
            detail={"agent_id": str(row[1])},
            decision_id=str(row[0]),
        )
        for row in cur.fetchall()
    ]


# --------------------------------------------------------------------------- #
# Penulisan
# --------------------------------------------------------------------------- #


def record(conn: psycopg.Connection, finding: Finding) -> bool:
    """Upsert satu kesimpulan. Audit **hanya** bila state-nya berubah.

    Mengembalikan ``True`` bila barisnya baru atau state-nya berganti.

    Siklus yang tidak menemukan perubahan karena itu tidak menulis satu baris
    audit pun. Itu penting: ``guard reconcile`` dijalankan berulang, dan
    ``audit_log`` yang dipenuhi "masih cocok" setiap lima menit berhenti bisa
    dibaca sebagai bukti.
    """
    with conn.cursor() as cur:
        cur.execute(SELECT_EXISTING, (finding.subject_type, finding.subject_id))
        row = cur.fetchone()
        previous = None if row is None else str(row[0])
        if previous == finding.state:
            conn.rollback()
            return False

        if row is None:
            cur.execute(
                INSERT_RECONCILIATION,
                (
                    finding.subject_type,
                    finding.subject_id,
                    finding.state,
                    finding.decision_id,
                    finding.execution_id,
                    finding.settlement_id,
                    Jsonb(finding.detail),
                ),
            )
        else:
            cur.execute(
                UPDATE_RECONCILIATION,
                (
                    finding.state,
                    Jsonb(finding.detail),
                    finding.subject_type,
                    finding.subject_id,
                ),
            )

        payload: dict[str, object] = {
            "subject_type": finding.subject_type,
            "subject_id": finding.subject_id,
            "state": finding.state,
            "detail": finding.detail,
        }
        if previous is not None:
            payload["previous_state"] = previous
        audit(cur, Event.RECONCILIATION_STATE_CHANGED, payload, actor=Actor.RECONCILER)
    conn.commit()
    return True


def run_reconcile(
    config: ReconcilerConfig,
    *,
    out: TextIO | None = None,
) -> int:
    """Satu siklus, dengan laporan. Kode keluar 1 bila ada alarm.

    Alarm di kode keluar, bukan hanya di keluaran: ``guard reconcile`` akan
    dijalankan dari cron atau dari CI, dan di sana yang dibaca adalah kode
    keluarnya.
    """
    report = reconcile_once(config, out=out)
    return 1 if report.alarms else 0


__all__ = [
    "ALARM_STATES",
    "BROADCAST_STATUSES",
    "RECONCILIATION_STATES",
    "STATE_EXECUTED_NOT_SETTLED",
    "STATE_EXPIRED",
    "STATE_INVALIDATED",
    "STATE_MATCHED",
    "STATE_MISMATCH",
    "STATE_UNMATCHED_ONCHAIN",
    "SUBJECT_DECISION",
    "SUBJECT_EXECUTION",
    "SUBJECT_SETTLEMENT",
    "SUBJECT_TYPES",
    "Finding",
    "ReconcileReport",
    "ReconcilerError",
    "collect_findings",
    "reconcile_once",
    "record",
    "run_reconcile",
]
