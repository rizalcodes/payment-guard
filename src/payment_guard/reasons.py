"""Status dan ``reason_code``: daftar tertutup, dan pasangan yang sah.

Daftar di sini **harus** sama persis dengan CHECK di ``migrations/0001_init.sql``
(komentar di file itu: "daftar tertutup, sama dengan enum di kode"). Ada tes
yang membaca dan mem-parse file SQL itu lalu membandingkannya dengan modul ini,
jadi keduanya tidak bisa menyimpang tanpa tes gagal.

Catatan satu penyimpangan tampilan: PRD v2.1 bagian 6.4 langkah 6 menulis
``approved (within_limits)``. ``within_limits`` **bukan** nama kode: ia tidak
ada di CHECK. Kode untuk langkah 6 adalah :data:`ReasonCode.APPROVED`.
Dikonfirmasi operator, 2 Nov 2026.

Modul ini murni: tidak menyentuh database, jaringan, atau jam.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Status(StrEnum):
    """Status keputusan. Sama dengan CHECK ``decisions.status``."""

    APPROVED = "approved"
    DENIED = "denied"
    NEEDS_HUMAN = "needs_human"
    REJECTED_BY_HUMAN = "rejected_by_human"
    EXPIRED = "expired"


class ReasonCode(StrEnum):
    """Alasan keputusan. Sama dengan CHECK ``decisions.reason_code``."""

    #: Langkah 6 PRD 6.4: lolos semua batas tanpa perlu manusia.
    APPROVED = "approved"
    #: Disetujui operator lewat Approvals CLI (PRD 6.5).
    APPROVED_BY_HUMAN = "approved_by_human"

    #: Masih dalam cooldown setelah penolakan sebelumnya.
    RETRY_BLOCKED = "retry_blocked"
    RECIPIENT_NOT_ALLOWED = "recipient_not_allowed"
    OVER_PER_TX_LIMIT = "over_per_tx_limit"
    OVER_DAILY_LIMIT = "over_daily_limit"
    #: Cek ulang saat approve gagal (PRD 6.5 langkah 3).
    RECHECK_OVER_PER_TX_LIMIT = "recheck_over_per_tx_limit"
    RECHECK_OVER_DAILY_LIMIT = "recheck_over_daily_limit"
    #: Mandate aktif berganti sebelum approve (PRD 6.5 langkah 2).
    MANDATE_CHANGED = "mandate_changed"

    #: Langkah 5 PRD 6.4: menunggu operator.
    HUMAN_APPROVAL_REQUIRED = "human_approval_required"
    HUMAN_REJECTED = "human_rejected"
    #: Batas tunggu manusia lewat (PRD 6.5 langkah 1).
    HUMAN_WAIT_TIMEOUT = "human_wait_timeout"


#: Pasangan status -> reason_code yang sah. Cermin CHECK ``status_reason_pairs``.
STATUS_REASONS: Final[dict[Status, frozenset[ReasonCode]]] = {
    Status.APPROVED: frozenset({ReasonCode.APPROVED, ReasonCode.APPROVED_BY_HUMAN}),
    Status.DENIED: frozenset(
        {
            ReasonCode.RETRY_BLOCKED,
            ReasonCode.RECIPIENT_NOT_ALLOWED,
            ReasonCode.OVER_PER_TX_LIMIT,
            ReasonCode.OVER_DAILY_LIMIT,
            ReasonCode.RECHECK_OVER_PER_TX_LIMIT,
            ReasonCode.RECHECK_OVER_DAILY_LIMIT,
            ReasonCode.MANDATE_CHANGED,
        }
    ),
    Status.NEEDS_HUMAN: frozenset({ReasonCode.HUMAN_APPROVAL_REQUIRED}),
    Status.REJECTED_BY_HUMAN: frozenset({ReasonCode.HUMAN_REJECTED}),
    Status.EXPIRED: frozenset({ReasonCode.HUMAN_WAIT_TIMEOUT}),
}

#: Penolakan ``denied`` yang TIDAK memicu cooldown (PRD 6.4, kalimat terakhir).
#:
#: ``retry_blocked`` dikecualikan supaya cooldown tidak memperpanjang dirinya
#: sendiri setiap kali agent mencoba lagi. ``mandate_changed`` dikecualikan
#: karena itu bukan kesalahan agent: operator yang mengganti mandate (ST-09).
NON_TRIGGERING_DENIALS: Final[frozenset[ReasonCode]] = frozenset(
    {ReasonCode.RETRY_BLOCKED, ReasonCode.MANDATE_CHANGED}
)


class ReasonPairError(ValueError):
    """Pasangan status dan ``reason_code`` tidak sah."""


def require_valid_pair(status: Status, reason_code: ReasonCode) -> None:
    """Pastikan ``(status, reason_code)`` ada di :data:`STATUS_REASONS`.

    Raises:
        ReasonPairError: bila pasangannya tidak sah. Database akan menolaknya
            juga lewat CHECK, tapi gagal di sini berarti gagal lebih awal dan
            dengan pesan yang lebih jelas.
    """
    if not isinstance(status, Status):
        raise ReasonPairError(f"status tidak dikenal: {status!r}")
    if not isinstance(reason_code, ReasonCode):
        raise ReasonPairError(f"reason_code tidak dikenal: {reason_code!r}")
    allowed = STATUS_REASONS[status]
    if reason_code not in allowed:
        raise ReasonPairError(
            f"pasangan tidak sah: status={status.value!r} dengan "
            f"reason_code={reason_code.value!r}. "
            f"Yang sah untuk status itu: {', '.join(sorted(c.value for c in allowed))}"
        )


def triggers_cooldown(status: Status, reason_code: ReasonCode) -> bool:
    """True bila keputusan ini memulai cooldown untuk (agent, recipient).

    PRD v2.1 bagian 6.4:

    * **Memicu:** ``denied`` selain ``retry_blocked`` dan ``mandate_changed``
      (termasuk kedua ``recheck_*``), dan ``rejected_by_human``.
    * **Tidak memicu:** ``retry_blocked``, ``mandate_changed``, ``expired``,
      dan tentu saja ``approved`` serta ``needs_human``.

    Fungsi ini adalah **satu-satunya** definisi aturan itu. Pemanggil memakainya
    untuk memilih baris penolakan mana yang relevan (lihat
    :class:`payment_guard.rules.Snapshot`), dan :func:`payment_guard.rules.evaluate`
    memakainya ulang untuk memeriksa apa yang dikirim pemanggil. Satu definisi,
    dua pemakai, tanpa peluang menyimpang.

    Raises:
        ReasonPairError: bila pasangannya tidak sah.
    """
    require_valid_pair(status, reason_code)
    if status is Status.REJECTED_BY_HUMAN:
        return True
    if status is Status.DENIED:
        return reason_code not in NON_TRIGGERING_DENIALS
    return False


def all_valid_pairs() -> frozenset[tuple[Status, ReasonCode]]:
    """Semua pasangan sah. Dipakai tes untuk perbandingan dua arah dengan SQL."""
    return frozenset(
        (status, reason_code) for status, codes in STATUS_REASONS.items() for reason_code in codes
    )


def cooldown_triggering_pairs() -> frozenset[tuple[Status, ReasonCode]]:
    """Pasangan yang memicu cooldown, diturunkan dari :func:`triggers_cooldown`.

    Diturunkan, bukan didaftar ulang: daftar kedua akan menyimpang suatu hari.
    """
    return frozenset(pair for pair in all_valid_pairs() if triggers_cooldown(pair[0], pair[1]))
