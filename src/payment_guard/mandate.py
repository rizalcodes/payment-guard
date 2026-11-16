"""Mandate: validasi, normalisasi, dan ``mandate_hash``.

PRD v2.1 bagian 6.1 dan Arsitektur v1.1 bagian 5.1.

``mandate_hash`` adalah identitas sebuah mandate. Ia mengikat setiap keputusan
ke aturan yang berlaku saat keputusan itu dibuat (``decisions.mandate_hash``),
dan Executor menolak execution yang hash-nya bukan mandate aktif (cek 4b).
Karena itu dua hal harus benar sekaligus:

* **Makna sama -> hash sama.** Urutan kunci, urutan allowlist, kasus huruf
  alamat, dan cara menulis jumlah uang tidak boleh mengubah hash (ST-07).
* **Makna beda -> hash beda.** Satu batas yang berubah, satu alamat yang
  berubah, satu detik TTL yang berubah: hash baru.

Bentuk file mandate, sebelas field, persis PRD v2 bagian 6.1::

    {
      "mandate_id": "m-001",
      "agent_id": "demo-agent",
      "chain": "base-sepolia",
      "max_per_tx": "50",
      "max_per_day": "200",
      "human_approval_above": "25",
      "allowed_recipients": ["0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"],
      "retry_cooldown_seconds": 600,
      "aggregation_window_seconds": 3600,
      "decision_ttl_seconds": 900,
      "human_wait_ttl_seconds": 3600
    }

Bentuk ternormalisasi (yang di-hash) memakai string base units untuk uang dan
alamat lowercase terurut.

Catatan penamaan: ``latest_triggering_denial`` di
:class:`payment_guard.rules.Snapshot` menggantikan ``latest_denial_at``
(PRD v2 bagian 14) karena penyaringan pemicu cooldown dilakukan pemanggil.

Modul ini murni: tidak menyentuh database, jaringan, atau jam.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from payment_guard.addresses import AddressError, normalize_allowlist
from payment_guard.canonical import canonical_bytes
from payment_guard.chains import NETWORK_PRODUCTION, is_supported_chain, supported_chains
from payment_guard.money import (
    BaseUnits,
    MoneyError,
    format_amount,
    format_base_units,
    parse_amount,
)

#: ``agent_id`` yang sah (PRD 6.1, ST-32).
#:
#: ``\A``/``\Z``, bukan ``^``/``$``: di Python ``$`` juga cocok tepat sebelum
#: newline di akhir string, jadi ``"demo-agent\n"`` akan lolos.
AGENT_ID_PATTERN: Final = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,63}\Z")

#: Panjang ``mandate_id`` yang sah. Cocok dengan CHECK di ``0001_init.sql``:
#: ``char_length(mandate_id) BETWEEN 1 AND 64``.
MANDATE_ID_MIN_LENGTH: Final = 1
MANDATE_ID_MAX_LENGTH: Final = 64

#: Awalan domain untuk hash mandate (PRD 6.1, Arsitektur 5.1).
#:
#: Awalan ini memisahkan ruang hash mandate dari ruang hash lain (mis. payload
#: envelope di M2), sehingga byte yang sah di satu konteks tidak bisa dipakai
#: ulang di konteks lain.
MANDATE_HASH_PREFIX: Final = b"payment-guard/mandate/v1\n"

#: Field uang, dinormalisasi menjadi string base units.
MONEY_FIELDS: Final[tuple[str, ...]] = (
    "max_per_tx",
    "max_per_day",
    "human_approval_above",
)

#: Field detik, wajib int > 0.
SECONDS_FIELDS: Final[tuple[str, ...]] = (
    "retry_cooldown_seconds",
    "aggregation_window_seconds",
    "decision_ttl_seconds",
    "human_wait_ttl_seconds",
)

#: Sebelas field PRD v2 bagian 6.1, tidak lebih dan tidak kurang.
REQUIRED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "mandate_id",
        "agent_id",
        "chain",
        "allowed_recipients",
        *MONEY_FIELDS,
        *SECONDS_FIELDS,
    }
)

#: Batas panjang nilai di pesan error, supaya log tetap terbaca.
_MAX_QUOTED_LENGTH: Final = 80


class MandateError(ValueError):
    """Mandate tidak sah. Error input saat load, bukan keputusan."""


@dataclass(frozen=True)
class Mandate:
    """Mandate yang sudah divalidasi dan dinormalisasi.

    Jumlah uang adalah ``int`` base units. ``allowed_recipients`` lowercase,
    terurut, tanpa duplikat. ``chain`` disimpan sebagai string PRD v2; nilai
    ``chain_id`` integer diturunkan lewat :mod:`payment_guard.chains` oleh
    pemakai yang membutuhkannya. Dibuat hanya lewat :func:`load_mandate`.
    """

    mandate_id: str
    agent_id: str
    chain: str
    allowed_recipients: tuple[str, ...]
    max_per_tx: BaseUnits
    max_per_day: BaseUnits
    human_approval_above: BaseUnits
    retry_cooldown_seconds: int
    aggregation_window_seconds: int
    decision_ttl_seconds: int
    human_wait_ttl_seconds: int

    def allows_recipient(self, recipient: str) -> bool:
        """True bila ``recipient`` (lowercase) ada di ``allowed_recipients``."""
        return recipient in self.allowed_recipients


def load_mandate(data: Mapping[str, object], *, network: str = NETWORK_PRODUCTION) -> Mandate:
    """Validasi dan normalisasi isi file mandate.

    Field yang tidak dikenal **ditolak**, bukan diabaikan. Field yang diabaikan
    akan membuat dua file berbeda menghasilkan ``mandate_hash`` yang sama,
    padahal operator mungkin bermaksud mengubah sesuatu.

    Raises:
        MandateError: dengan pesan yang menyebut nama field yang bermasalah.
    """
    if not isinstance(data, Mapping):
        raise MandateError(f"mandate harus berupa objek, bukan {type(data).__name__}")

    present = set(data)
    missing = REQUIRED_FIELDS - present
    if missing:
        raise MandateError(f"field wajib tidak ada: {', '.join(sorted(missing))}")
    unknown = present - REQUIRED_FIELDS
    if unknown:
        raise MandateError(
            f"field tidak dikenal: {', '.join(sorted(unknown))}. "
            "Field yang diabaikan akan membuat dua mandate berbeda punya hash sama."
        )

    mandate_id = _require_mandate_id(data["mandate_id"])
    agent_id = _require_agent_id(data["agent_id"])
    chain = _require_chain(data["chain"], network=network)
    allowed_recipients = _require_allowed_recipients(data["allowed_recipients"])
    amounts = {field: _require_money(field, data[field]) for field in MONEY_FIELDS}
    seconds = {field: _require_seconds(field, data[field]) for field in SECONDS_FIELDS}

    _require_limit_ordering(amounts)

    return Mandate(
        mandate_id=mandate_id,
        agent_id=agent_id,
        chain=chain,
        allowed_recipients=allowed_recipients,
        max_per_tx=amounts["max_per_tx"],
        max_per_day=amounts["max_per_day"],
        human_approval_above=amounts["human_approval_above"],
        retry_cooldown_seconds=seconds["retry_cooldown_seconds"],
        aggregation_window_seconds=seconds["aggregation_window_seconds"],
        decision_ttl_seconds=seconds["decision_ttl_seconds"],
        human_wait_ttl_seconds=seconds["human_wait_ttl_seconds"],
    )


def normalized_form(mandate: Mandate) -> dict[str, object]:
    """Bentuk kanonik mandate: uang sebagai string base units, alamat lowercase.

    Hanya ``str``, ``int``, dan ``list[str]`` yang muncul, supaya
    :func:`payment_guard.canonical.canonical_bytes` menerimanya.

    Urutan kunci di sini tidak berarti apa-apa: ``canonical_bytes`` yang
    mengurutkannya. Jangan menulis urutan secara manual, karena urutan manual
    akan menyimpang begitu ada field baru.
    """
    return {
        "mandate_id": mandate.mandate_id,
        "agent_id": mandate.agent_id,
        "chain": mandate.chain,
        "allowed_recipients": list(mandate.allowed_recipients),
        "max_per_tx": format_base_units(mandate.max_per_tx),
        "max_per_day": format_base_units(mandate.max_per_day),
        "human_approval_above": format_base_units(mandate.human_approval_above),
        "retry_cooldown_seconds": mandate.retry_cooldown_seconds,
        "aggregation_window_seconds": mandate.aggregation_window_seconds,
        "decision_ttl_seconds": mandate.decision_ttl_seconds,
        "human_wait_ttl_seconds": mandate.human_wait_ttl_seconds,
    }


def load_normalized(document: Mapping[str, object]) -> Mandate:
    """Kebalikan dari :func:`normalized_form`.

    Dipakai pembaca yang mengambil mandate dari kolom ``mandates.content``,
    bukan dari berkas: Approvals CLI (M4) dan, nanti, Reconciler. Keduanya
    tidak punya berkas mandate, dan yang mengikat memang mandate yang sudah
    dimuat Guard, bukan yang ada di disk (Arsitektur v1.1 bagian 5.4).

    Perbedaannya hanya pada satuan uang, dan perbedaan itu halus justru karena
    kedua bentuk sama-sama string angka. ``normalized_form`` menulis **base
    units** (``"60000000"``), sedangkan :func:`load_mandate` membaca **desimal
    USDC** (``"60"``). Membaca yang satu dengan pembaca yang lain tidak gagal;
    ia diam-diam mengalikan setiap batas dengan sejuta. Karena itu konversinya
    ada di satu tempat, dengan tes bolak-balik yang mengunci keduanya.

    Raises:
        MandateError: bila bentuknya tidak sah.
    """
    if not isinstance(document, Mapping):
        raise MandateError(f"mandate harus objek, bukan {type(document).__name__}")

    data = dict(document)
    for field in MONEY_FIELDS:
        if field not in data:
            continue
        data[field] = _decimal_from_base_units(field, data[field])
    return load_mandate(data)


def _decimal_from_base_units(field: str, value: object) -> str:
    """Ubah ``"60000000"`` menjadi ``"60.000000"`` agar bisa dibaca ulang."""
    if not isinstance(value, str):
        raise MandateError(f"{field} harus string base units, bukan {type(value).__name__}")
    if not value.isascii() or not value.isdigit():
        raise MandateError(f"{field} harus angka bulat base units, dapat {_quote(value)}")
    return format_amount(int(value))


def normalized_bytes(mandate: Mandate) -> bytes:
    """Byte kanonik dari bentuk ternormalisasi, tanpa awalan domain."""
    return canonical_bytes(normalized_form(mandate))


def compute_mandate_hash(mandate: Mandate) -> str:
    """``mandate_hash``: SHA-256 heksadesimal lowercase, 64 karakter.

    Cocok dengan CHECK ``mandates.mandate_hash ~ '^[0-9a-f]{64}$'``.
    """
    digest = hashlib.sha256(MANDATE_HASH_PREFIX + normalized_bytes(mandate))
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Validasi per field
# --------------------------------------------------------------------------- #


def _quote(value: object) -> str:
    """Kutip nilai untuk pesan error, dipotong bila panjang.

    Mandate tidak memuat secret, tapi nilai mentah yang panjang tetap tidak
    ditulis utuh ke pesan error: pesan error berakhir di log dan di terminal.
    """
    text = repr(value)
    if len(text) <= _MAX_QUOTED_LENGTH:
        return text
    return text[:_MAX_QUOTED_LENGTH] + "... (dipotong)"


def _require_text(field: str, value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise MandateError(f"{field} harus berupa string, bukan {type(value).__name__}")
    return value


def _require_mandate_id(value: object) -> str:
    text = _require_text("mandate_id", value)
    if not MANDATE_ID_MIN_LENGTH <= len(text) <= MANDATE_ID_MAX_LENGTH:
        raise MandateError(
            f"mandate_id harus {MANDATE_ID_MIN_LENGTH}..{MANDATE_ID_MAX_LENGTH} karakter, "
            f"dapat {len(text)}"
        )
    if any(character.isspace() or not character.isprintable() for character in text):
        raise MandateError(
            f"mandate_id tidak boleh memuat spasi atau karakter kontrol: {_quote(text)}"
        )
    return text


def _require_agent_id(value: object) -> str:
    """ST-32: tolak ``:``, spasi, huruf besar, dan lebih dari 64 karakter."""
    text = _require_text("agent_id", value)
    if AGENT_ID_PATTERN.match(text) is None:
        raise MandateError(
            f"agent_id {_quote(text)} tidak sah: harus cocok "
            r"^[a-z0-9][a-z0-9_-]{0,63}$ "
            "(huruf kecil, angka, garis bawah, tanda hubung; mulai dengan "
            "huruf kecil atau angka; maksimum 64 karakter)"
        )
    return text


def _require_chain(value: object, *, network: str) -> str:
    """PRD v2 bagian 6.2: ``chain`` hanya ``base-sepolia`` di produksi.

    Mandate menyimpan string ini apa adanya. Pemetaan ke ``chain_id`` EIP-155
    ada di :mod:`payment_guard.chains`, bukan di mandate.

    Di jaringan devnet lokal yang berlaku adalah ``devnet-local``, dan
    ``base-sepolia`` **ditolak** di sana — penolakan itu dua arah dengan
    sengaja: mandate produksi yang dimuat di devnet akan menandatangani
    ``chain_id`` 84532 untuk transaksi yang dikirim ke chain 31337.
    """
    text = _require_text("chain", value)
    if not is_supported_chain(text, network=network):
        raise MandateError(
            f"chain {_quote(text)} tidak didukung di jaringan {network!r}; "
            f"yang didukung: {', '.join(supported_chains(network=network))}"
        )
    return text


def _require_allowed_recipients(value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, list):
        raise MandateError(
            f"allowed_recipients harus berupa list alamat, bukan {type(value).__name__}"
        )
    try:
        return tuple(normalize_allowlist(value))
    except AddressError as exc:
        raise MandateError(f"allowed_recipients: {exc}") from None


def _require_money(field: str, value: object) -> BaseUnits:
    try:
        return parse_amount(value)  # type: ignore[arg-type]
    except MoneyError as exc:
        raise MandateError(f"{field}: {exc}") from None


def _require_seconds(field: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MandateError(f"{field} harus berupa int detik, bukan {type(value).__name__}")
    if value <= 0:
        raise MandateError(f"{field} harus lebih besar dari nol, dapat {value}")
    return value


def _require_limit_ordering(amounts: Mapping[str, BaseUnits]) -> None:
    """Tegakkan ``0 < human_approval_above <= max_per_tx <= max_per_day``.

    Urutan ini membuat setiap batas punya arti. Kalau
    ``human_approval_above > max_per_tx``, langkah 5 PRD 6.4 tidak akan pernah
    tercapai dan tidak ada pembayaran yang pernah minta persetujuan manusia.
    Kalau ``max_per_tx > max_per_day``, batas per transaksi tidak berarti.
    """
    above = amounts["human_approval_above"]
    per_tx = amounts["max_per_tx"]
    per_day = amounts["max_per_day"]

    if above > per_tx:
        raise MandateError(
            f"human_approval_above ({above}) tidak boleh lebih besar dari "
            f"max_per_tx ({per_tx}); kalau tidak, tidak ada pembayaran yang "
            "pernah minta persetujuan manusia"
        )
    if per_tx > per_day:
        raise MandateError(
            f"max_per_tx ({per_tx}) tidak boleh lebih besar dari max_per_day ({per_day})"
        )
