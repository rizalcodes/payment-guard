"""Aturan keputusan. Fungsi murni, urutan evaluasi tetap.

:func:`evaluate` adalah satu-satunya tempat aturan mandate ditegakkan. Ia
**murni**: tidak menyentuh database, jaringan, atau jam. Semua yang berasal
dari dunia luar disuntikkan sebagai parameter — termasuk waktu, yang selalu
jam database (Arsitektur v1.1 prinsip 5), bukan jam proses ini.

Dua hal yang sengaja TIDAK ada di :class:`Request`:

``reason``
    Field ``reason`` dari agent tidak pernah memengaruhi keputusan (PRD v2.1
    bagian 3 nomor 10, Security v1.1 E1/ST-21). Cara paling kuat untuk
    menjaminnya bukan disiplin, tapi membuatnya mustahil: :func:`evaluate`
    tidak punya parameter untuk itu, jadi tidak ada yang bisa membacanya di
    sini. Ada tes yang memeriksa signature fungsi ini.

``agent_id``
    Identitas datang dari mandate, bukan dari permintaan (Security v1.1 S1).

Urutan evaluasi mengikuti PRD v2.1 bagian 6.4 persis, dan urutannya bermakna:
cooldown diperiksa lebih dulu supaya agent yang baru ditolak tidak bisa
memetakan batas dengan mencoba berulang kali.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from payment_guard.addresses import ADDRESS_PATTERN
from payment_guard.mandate import Mandate
from payment_guard.reasons import (
    ReasonCode,
    Status,
    require_valid_pair,
    triggers_cooldown,
)

__all__ = [
    "Decision",
    "Denial",
    "Request",
    "RuleInputError",
    "Snapshot",
    "evaluate",
]

#: Batas bawah waktu yang diterima. Nol atau negatif hampir selalu berarti
#: pemanggil lupa menyuntikkan jam database.
_MIN_NOW: Final = 1


class RuleInputError(ValueError):
    """Masukan ke :func:`evaluate` tidak sah.

    Error input (PRD 6.4 langkah 0), bukan keputusan: tidak ada baris yang
    ditulis ke ``decisions``.
    """


@dataclass(frozen=True)
class Request:
    """Permintaan pembayaran yang sudah divalidasi pemanggil.

    ``recipient`` sudah ternormalisasi lowercase oleh
    :func:`payment_guard.addresses.normalize_address`, dan ``amount_base``
    sudah berupa ``int`` base units dari
    :func:`payment_guard.money.parse_amount`.
    """

    recipient: str
    amount_base: int
    chain_id: int


@dataclass(frozen=True)
class Denial:
    """Satu penolakan sebelumnya untuk (agent, recipient) yang sama.

    ``decided_at`` dalam detik UTC, skala yang sama dengan ``now``.
    """

    decided_at: int
    status: Status
    reason_code: ReasonCode


@dataclass(frozen=True)
class Snapshot:
    """Keadaan yang sudah dihitung pemanggil, di dalam lock ``agent:<agent_id>``.

    ``latest_triggering_denial`` adalah penolakan **pemicu cooldown** terakhir
    untuk (agent, recipient) ini — bukan penolakan terakhir apa pun. Pemanggil
    menyaringnya dengan :func:`payment_guard.reasons.triggers_cooldown`, fungsi
    yang sama yang dipakai di sini.

    Penyaringan itu bukan detail: kalau pemanggil mengirim penolakan terakhir
    apa pun, satu baris ``retry_blocked`` yang lebih baru akan menutupi pemicu
    aslinya dan cooldown justru berakhir lebih cepat. PRD 6.4 menuntut
    sebaliknya — ``retry_blocked`` tidak memperpanjang, dan juga tidak
    memperpendek. :func:`evaluate` memeriksa ulang apa yang dikirim dan
    mengabaikan yang bukan pemicu, tapi pemilihan baris tetap tugas pemanggil.
    """

    committed_day: int
    committed_recipient: int
    latest_triggering_denial: Denial | None = None


@dataclass(frozen=True)
class Decision:
    """Hasil :func:`evaluate`.

    Tidak membawa ``envelope``: penandatanganan adalah M2, dan aturan tidak
    perlu tahu soal kunci.

    Invarian yang ditegakkan saat konstruksi:

    * pasangan ``status``/``reason_code`` harus sah (cermin CHECK
      ``status_reason_pairs`` di ``0001_init.sql``)
    * ``retry_after`` terisi **tepat** untuk ``retry_blocked``, dan harus > 0
    """

    status: Status
    reason_code: ReasonCode
    retry_after: int | None = None

    def __post_init__(self) -> None:
        require_valid_pair(self.status, self.reason_code)

        is_retry_blocked = self.reason_code is ReasonCode.RETRY_BLOCKED
        if is_retry_blocked and self.retry_after is None:
            raise ValueError("retry_blocked wajib membawa retry_after")
        if not is_retry_blocked and self.retry_after is not None:
            raise ValueError(
                f"retry_after hanya untuk retry_blocked, bukan {self.reason_code.value!r}"
            )
        if self.retry_after is not None and self.retry_after <= 0:
            raise ValueError(f"retry_after harus lebih besar dari nol, dapat {self.retry_after}")


def evaluate(
    request: Request,
    mandate: Mandate,
    snapshot: Snapshot,
    now: int,
) -> Decision:
    """Terapkan aturan mandate. Murni: hasil hanya bergantung pada argumen.

    Urutan persis PRD v2.1 bagian 6.4. Semua batas **inklusif di sisi boleh**:
    jumlah yang tepat sama dengan batas tetap lolos.

    Args:
        request: permintaan yang sudah divalidasi.
        mandate: mandate aktif, sudah ternormalisasi.
        snapshot: jatah terpakai dan penolakan pemicu terakhir.
        now: detik UTC dari **jam database**, disuntikkan pemanggil.

    Returns:
        Keputusan, tanpa efek samping.

    Raises:
        RuleInputError: bila argumennya tidak sesuai kontrak.
    """
    _require_inputs(request, mandate, snapshot, now)

    # 1. Cooldown setelah penolakan sebelumnya.
    denial = snapshot.latest_triggering_denial
    if denial is not None and triggers_cooldown(denial.status, denial.reason_code):
        cooldown_ends = denial.decided_at + mandate.retry_cooldown_seconds
        if now < cooldown_ends:
            return Decision(
                status=Status.DENIED,
                reason_code=ReasonCode.RETRY_BLOCKED,
                retry_after=cooldown_ends - now,
            )

    # 2. Recipient harus ada di allowlist.
    if not mandate.allows_recipient(request.recipient):
        return Decision(Status.DENIED, ReasonCode.RECIPIENT_NOT_ALLOWED)

    # 3. Batas per transaksi.
    if request.amount_base > mandate.max_per_tx:
        return Decision(Status.DENIED, ReasonCode.OVER_PER_TX_LIMIT)

    # 4. Batas harian, termasuk yang sudah terpakai hari ini.
    if snapshot.committed_day + request.amount_base > mandate.max_per_day:
        return Decision(Status.DENIED, ReasonCode.OVER_DAILY_LIMIT)

    # 5. Ambang persetujuan manusia, diagregasi per recipient (ST-12).
    if snapshot.committed_recipient + request.amount_base > mandate.human_approval_above:
        return Decision(Status.NEEDS_HUMAN, ReasonCode.HUMAN_APPROVAL_REQUIRED)

    # 6. Lolos semua batas.
    return Decision(Status.APPROVED, ReasonCode.APPROVED)


def cooldown_ends_at(denial: Denial, mandate: Mandate) -> int:
    """Detik UTC saat cooldown dari ``denial`` berakhir.

    Dipisah supaya pemanggil (dan tes batas C-1 / C / C+1) tidak perlu
    menghitung ulang aritmetikanya sendiri.
    """
    return denial.decided_at + mandate.retry_cooldown_seconds


def _require_inputs(request: Request, mandate: Mandate, snapshot: Snapshot, now: int) -> None:
    if not isinstance(request, Request):
        raise RuleInputError(f"request harus Request, bukan {type(request).__name__}")
    if not isinstance(mandate, Mandate):
        raise RuleInputError(f"mandate harus Mandate, bukan {type(mandate).__name__}")
    if not isinstance(snapshot, Snapshot):
        raise RuleInputError(f"snapshot harus Snapshot, bukan {type(snapshot).__name__}")

    _require_non_negative_int("now", now)
    if now < _MIN_NOW:
        raise RuleInputError(
            f"now harus detik UTC dari jam database, dapat {now}; "
            "nilai nol atau negatif biasanya berarti jam belum disuntikkan"
        )

    if isinstance(request.amount_base, bool) or not isinstance(request.amount_base, int):
        raise RuleInputError(
            f"amount_base harus int base units, bukan {type(request.amount_base).__name__}"
        )
    if request.amount_base <= 0:
        raise RuleInputError(f"amount_base harus lebih besar dari nol, dapat {request.amount_base}")

    if not isinstance(request.recipient, str) or ADDRESS_PATTERN.match(request.recipient) is None:
        raise RuleInputError("recipient harus alamat '0x' + 40 hex yang sudah divalidasi")
    if request.recipient != request.recipient.lower():
        raise RuleInputError(
            f"recipient harus sudah ternormalisasi lowercase, dapat {request.recipient!r}"
        )

    _require_non_negative_int("committed_day", snapshot.committed_day)
    _require_non_negative_int("committed_recipient", snapshot.committed_recipient)

    denial = snapshot.latest_triggering_denial
    if denial is not None:
        if not isinstance(denial, Denial):
            raise RuleInputError(
                f"latest_triggering_denial harus Denial, bukan {type(denial).__name__}"
            )
        _require_non_negative_int("latest_triggering_denial.decided_at", denial.decided_at)


def _require_non_negative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuleInputError(f"{name} harus int, bukan {type(value).__name__}")
    if value < 0:
        raise RuleInputError(f"{name} tidak boleh negatif, dapat {value}")
