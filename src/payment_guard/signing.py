"""Envelope bertanda tangan: Ed25519, bentuk kanonik, dan parser ketat.

PRD v2 bagian 7, PRD v2.1 bagian 7 dan 9, Security v1.1 S2, S3, E4, E5.

Envelope adalah kredensial sekali pakai: ia menyatakan "Guard menyetujui
pembayaran ini". Executor hanya memegang **public key**, jadi ia bisa
memeriksa pernyataan itu tanpa bisa membuatnya (Arsitektur v1.1 bagian 2).

Bentuk::

    {
      "payload": { ...sebelas field... },
      "key_id": "guard-2026-11-a1b2",
      "signature": "<base64>"
    }

Yang ditandatangani = :data:`SIGNING_DOMAIN_PREFIX` + ``canonical_bytes(payload)``.
Serialisasi kanoniknya **dipakai ulang** dari :mod:`payment_guard.canonical`,
bukan ditulis ulang di sini: Arsitektur v1.1 bagian 5.1 mewajibkan "fungsi
kanonik sama dengan signing", dan dua implementasi akan menyimpang suatu hari.

Pembagian tugas parse dan verifikasi
------------------------------------

:func:`parse_envelope` menegakkan **bentuk dan tipe** saja. :func:`verify`
menegakkan **nilai**. Pembagian itu bukan selera: PRD v2.1 bagian 9 memberi
alasan penolakan yang berbeda untuk keduanya. Parse ketat yang gagal beralasan
``signature_invalid`` (cek 3), sedangkan ``v``/``chain_id``/``status`` yang
tidak cocok beralasan ``version_or_chain_mismatch`` atau ``not_approved``
(cek 2). Kalau parser ikut menolak ``v`` yang salah, Executor akan melaporkan
alasan yang keliru.

Verifikasi selalu dihitung atas ``canonical_bytes`` dari payload **hasil
parse**, bukan atas byte mentah file. Jadi spasi, indentasi, dan urutan kunci
di file tidak mengubah hasil (ST-05), dan tidak ada celah di mana byte yang
diverifikasi berbeda dari nilai yang dibaca.

Modul ini murni: tidak menyentuh database, jaringan, jam, atau berkas.
Pembuatan kunci ada di :mod:`payment_guard.keygen`, karena itu menulis berkas.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, NoReturn

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from payment_guard.canonical import CanonicalError, canonical_bytes
from payment_guard.chains import BASE_SEPOLIA, chain_id_for
from payment_guard.money import BaseUnits, format_base_units

#: Awalan domain untuk tanda tangan envelope (PRD v2 bagian 7).
#:
#: Awalan memisahkan ruang tanda tangan envelope dari ruang hash lain, mis.
#: ``payment-guard/mandate/v1\n`` di :mod:`payment_guard.mandate`. Tanpa
#: awalan, byte yang sah di satu konteks bisa dipakai ulang di konteks lain.
SIGNING_DOMAIN_PREFIX: Final = b"payment-guard/v1\n"

#: Nilai field ``v`` yang sah. Satu versi, daftar tertutup.
ENVELOPE_VERSION: Final = "payment-guard/v1"

#: Satu-satunya status yang boleh ditandatangani (PRD v2 bagian 7).
SIGNED_STATUS: Final = "approved"

#: Panjang kunci dan tanda tangan Ed25519, dalam byte.
ED25519_KEY_SIZE: Final = 32
ED25519_SIGNATURE_SIZE: Final = 64

#: Field payload dan tipe Python-nya. Daftar tertutup: tidak ada field
#: opsional, dan tidak ada field tambahan yang diterima.
#:
#: Perhatikan ``amount_base`` adalah **string**, bukan int: JSON tidak menjamin
#: bilangan bulat besar melewati parser mana pun tanpa kehilangan presisi,
#: sedangkan string selalu persis. ``chain_id``, ``issued_at``, dan
#: ``valid_until`` adalah int, sesuai contoh di PRD v2 bagian 7.
PAYLOAD_TYPES: Final[dict[str, type]] = {
    "v": str,
    "decision_id": str,
    "mandate_id": str,
    "agent_id": str,
    "chain_id": int,
    "recipient": str,
    "amount_base": str,
    "status": str,
    "issued_at": int,
    "valid_until": int,
    "mandate_hash": str,
}

#: Field envelope tingkat atas. Tidak ada field ``alg``: hanya Ed25519 yang
#: didukung, dan field algoritma yang bisa dipilih penyerang adalah salah satu
#: cara klasik melemahkan verifikasi (Security v1.1 E5).
ENVELOPE_TYPES: Final[dict[str, type]] = {
    "payload": dict,
    "key_id": str,
    "signature": str,
}

#: Bentuk nilai yang dibatasi. ``\A``/``\Z``, bukan ``^``/``$``: di Python
#: ``$`` juga cocok tepat sebelum newline di akhir string.
RECIPIENT_PATTERN: Final = re.compile(r"\A0x[0-9a-f]{40}\Z")
MANDATE_HASH_PATTERN: Final = re.compile(r"\A[0-9a-f]{64}\Z")
DECISION_ID_PATTERN: Final = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
AMOUNT_BASE_PATTERN: Final = re.compile(r"\A(0|[1-9][0-9]*)\Z")
KEY_ID_PATTERN: Final = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,63}\Z")

#: Batas panjang untuk field string yang isinya tidak dibatasi pola.
_MAX_OPAQUE_LENGTH: Final = 64


class SigningError(ValueError):
    """Tidak bisa menandatangani. Kesalahan pemanggil, bukan keputusan."""


class EnvelopeError(ValueError):
    """Envelope tidak bisa di-parse. Bentuk atau tipe salah.

    PRD v2.1 bagian 9 cek 3: alasan penolakannya ``signature_invalid``.
    """


class VerificationError(ValueError):
    """Envelope ter-parse tapi tidak lolos verifikasi.

    Pesannya ditampilkan ke operator, jadi ia tidak pernah memuat bahan kunci.
    """


# --------------------------------------------------------------------------- #
# Payload dan envelope
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Payload:
    """Isi envelope yang ditandatangani. Sebelas field, semuanya wajib."""

    v: str
    decision_id: str
    mandate_id: str
    agent_id: str
    chain_id: int
    recipient: str
    amount_base: str
    status: str
    issued_at: int
    valid_until: int
    mandate_hash: str

    def as_dict(self) -> dict[str, object]:
        """Bentuk dict untuk diserialisasi kanonik.

        Urutan kunci di sini tidak berarti apa-apa: ``canonical_bytes`` yang
        mengurutkannya.
        """
        return {name: getattr(self, name) for name in PAYLOAD_TYPES}


@dataclass(frozen=True)
class Envelope:
    """Payload, ``key_id`` yang menandatanganinya, dan tanda tangannya."""

    payload: Payload
    key_id: str
    signature: str

    def as_dict(self) -> dict[str, object]:
        return {
            "payload": self.payload.as_dict(),
            "key_id": self.key_id,
            "signature": self.signature,
        }


@dataclass(frozen=True)
class PublicKeyEntry:
    """Satu baris di ``guard_public.json``.

    ``revoked`` adalah bool JSON biasa. Berkas ini **tidak** ditandatangani
    dan tidak pernah diserialisasi kanonik, jadi larangan bool di
    :mod:`payment_guard.canonical` tidak berlaku untuknya.
    """

    key_id: str
    public_key: str
    revoked: bool


def build_payload(
    *,
    decision_id: str,
    mandate_id: str,
    agent_id: str,
    recipient: str,
    amount_base: BaseUnits,
    issued_at: int,
    valid_until: int,
    mandate_hash: str,
    chain: str = BASE_SEPOLIA,
    status: str = SIGNED_STATUS,
) -> Payload:
    """Susun payload dari nilai internal.

    ``amount_base`` masuk sebagai ``int`` base units dan keluar sebagai string
    lewat :func:`payment_guard.money.format_base_units`, supaya tidak ada
    konversi int-ke-string kedua di basis kode ini.

    ``chain_id`` diturunkan dari nama chain lewat
    :func:`payment_guard.chains.chain_id_for`; angkanya tidak ditulis di modul
    ini.

    Raises:
        SigningError: bila ``status`` bukan ``approved``, atau bila ada nilai
            yang bentuknya tidak sah.
    """
    _require_signable_status(status)
    payload = Payload(
        v=ENVELOPE_VERSION,
        decision_id=decision_id,
        mandate_id=mandate_id,
        agent_id=agent_id,
        chain_id=chain_id_for(chain),
        recipient=recipient,
        amount_base=format_base_units(amount_base),
        status=status,
        issued_at=issued_at,
        valid_until=valid_until,
        mandate_hash=mandate_hash,
    )
    _validate_payload_values(payload, error=SigningError)
    return payload


def signing_bytes(payload: Payload) -> bytes:
    """Byte yang ditandatangani: awalan domain + byte kanonik payload."""
    return SIGNING_DOMAIN_PREFIX + canonical_bytes(payload.as_dict())


def sign(payload: Payload, signing_key: Ed25519PrivateKey, key_id: str) -> Envelope:
    """Tandatangani ``payload``. Satu-satunya fungsi yang butuh kunci rahasia.

    Raises:
        SigningError: bila status bukan ``approved``, ``key_id`` tidak sah,
            atau kunci bukan Ed25519.
    """
    if not isinstance(payload, Payload):
        raise SigningError(f"payload harus Payload, bukan {type(payload).__name__}")
    _require_signable_status(payload.status)
    _validate_payload_values(payload, error=SigningError)
    _require_key_id(key_id, error=SigningError)
    if not isinstance(signing_key, Ed25519PrivateKey):
        raise SigningError("kunci penanda tangan harus Ed25519")

    signature = signing_key.sign(signing_bytes(payload))
    return Envelope(
        payload=payload,
        key_id=key_id,
        signature=base64.b64encode(signature).decode("ascii"),
    )


# --------------------------------------------------------------------------- #
# Parser ketat (ST-05, Security v1.1 E4)
# --------------------------------------------------------------------------- #


def _decode_text(raw: bytes | str, label: str) -> str:
    """Dekode masukan mentah menjadi teks UTF-8.

    Dipisah karena dekode harus ikut terbungkus penanganan error: byte yang
    bukan UTF-8 adalah masukan tidak sah, bukan kesalahan program, jadi ia
    harus keluar sebagai :class:`EnvelopeError` seperti pelanggaran lain.
    """
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, bytes | bytearray | memoryview):
        raise EnvelopeError(f"{label} harus bytes atau str, bukan {type(raw).__name__}")
    try:
        return bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        raise EnvelopeError(f"{label} bukan UTF-8 yang sah") from None


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Hook ``json`` yang menolak kunci ganda.

    ``json.loads`` secara bawaan menyimpan kunci terakhir dan membuang yang
    lain tanpa suara. Itu berarti dua pembaca bisa melihat dua nilai berbeda
    dari byte yang sama, yang merupakan kelemahan verifikasi, bukan sekadar
    kerapian.
    """
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise EnvelopeError(f"kunci ganda di JSON: {key!r}")
        seen.add(key)
    return dict(pairs)


def _reject_non_integer_number(value: str) -> NoReturn:
    """Hook ``json`` untuk angka non-integer. Selalu melempar.

    Dipasang sebagai ``parse_float``, jadi ``1.0`` dan ``1e3`` di JSON tertolak
    di lapisan parser, sebelum ada nilai yang bisa dipakai siapa pun.
    """
    raise EnvelopeError(f"angka non-integer tidak diizinkan di envelope: {value}")


def parse_envelope(raw: bytes | str) -> Envelope:
    """Parse dan validasi envelope dari JSON mentah. Ketat di setiap lapis.

    Yang ditolak: JSON tidak sah, kunci ganda, field tingkat atas yang hilang
    atau berlebih, field payload yang hilang atau berlebih, tipe yang salah
    (termasuk ``bool`` yang di Python adalah subclass ``int``), angka
    non-integer di mana pun, dan nilai yang bentuknya tidak sesuai pola.

    Yang **tidak** diperiksa di sini: nilai ``v``, ``chain_id``, dan
    ``status``. Itu tugas :func:`verify`; lihat catatan di docstring modul.

    Raises:
        EnvelopeError: pada pelanggaran pertama yang ditemukan.
    """
    text = _decode_text(raw, "envelope")
    try:
        document = json.loads(
            text, object_pairs_hook=_no_duplicate_keys, parse_float=_reject_non_integer_number
        )
    except EnvelopeError:
        raise
    except json.JSONDecodeError as exc:
        raise EnvelopeError(f"envelope bukan JSON yang sah: {exc.msg}") from None

    _require_exact_object("envelope", document, ENVELOPE_TYPES)
    payload_object = document["payload"]
    _require_exact_object("payload", payload_object, PAYLOAD_TYPES)

    _require_key_id(document["key_id"], error=EnvelopeError)
    _require_signature_shape(document["signature"])

    payload = Payload(**{name: payload_object[name] for name in PAYLOAD_TYPES})
    _validate_payload_values(payload, error=EnvelopeError)
    return Envelope(payload=payload, key_id=document["key_id"], signature=document["signature"])


def dump_envelope(envelope: Envelope) -> bytes:
    """Serialisasi envelope untuk ditulis ke berkas.

    Memakai bentuk kanonik yang sama dengan yang ditandatangani, jadi berkas
    yang dihasilkan selalu identik untuk envelope yang sama. Tidak diwajibkan
    dokumen, tapi membuat perbandingan berkas berarti.
    """
    return canonical_bytes(envelope.as_dict())


def parse_public_keys(raw: bytes | str) -> dict[str, PublicKeyEntry]:
    """Parse ``guard_public.json`` menjadi registri ``key_id`` -> entri.

    Bentuk yang diterima::

        {"keys": [{"key_id": "...", "public_key": "<base64>", "revoked": false}]}

    Raises:
        EnvelopeError: bila bentuknya salah, ada ``key_id`` ganda, atau ada
            bahan kunci yang tidak bisa didekode.
    """
    text = _decode_text(raw, "berkas kunci publik")
    try:
        document = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except EnvelopeError:
        raise
    except json.JSONDecodeError as exc:
        raise EnvelopeError(f"berkas kunci publik bukan JSON yang sah: {exc.msg}") from None

    if not isinstance(document, dict) or set(document) != {"keys"}:
        raise EnvelopeError("berkas kunci publik harus objek dengan satu kunci: 'keys'")
    entries = document["keys"]
    if not isinstance(entries, list) or not entries:
        raise EnvelopeError("'keys' harus list yang tidak kosong")

    registry: dict[str, PublicKeyEntry] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"key_id", "public_key", "revoked"}:
            raise EnvelopeError(f"keys[{index}] harus memuat tepat: key_id, public_key, revoked")
        key_id, material, revoked = entry["key_id"], entry["public_key"], entry["revoked"]
        _require_key_id(key_id, error=EnvelopeError)
        if not isinstance(revoked, bool):
            raise EnvelopeError(f"keys[{index}].revoked harus true atau false")
        if not isinstance(material, str):
            raise EnvelopeError(f"keys[{index}].public_key harus string base64")
        # Dekode sekarang supaya berkas yang rusak gagal saat dibaca, bukan
        # nanti saat verifikasi.
        decode_public_key(material)
        if key_id in registry:
            raise EnvelopeError(f"key_id ganda di berkas kunci publik: {key_id!r}")
        registry[key_id] = PublicKeyEntry(key_id=key_id, public_key=material, revoked=revoked)
    return registry


def decode_public_key(material: str) -> Ed25519PublicKey:
    """Dekode public key Ed25519 dari base64 standar.

    Raises:
        EnvelopeError: bila base64-nya tidak sah atau panjangnya salah. Pesan
            tidak memuat bahan kunci.
    """
    raw = _decode_base64(material, "public_key")
    if len(raw) != ED25519_KEY_SIZE:
        raise EnvelopeError(f"public key harus {ED25519_KEY_SIZE} byte, dapat {len(raw)} byte")
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError:
        raise EnvelopeError("public key bukan titik Ed25519 yang sah") from None


def encode_public_key(public_key: Ed25519PublicKey) -> str:
    """Public key Ed25519 sebagai base64 standar, tanpa newline."""
    raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


# --------------------------------------------------------------------------- #
# Verifikasi (hanya butuh public key)
# --------------------------------------------------------------------------- #


def resolve_key(registry: Mapping[str, PublicKeyEntry], key_id: str) -> Ed25519PublicKey:
    """Cari ``key_id`` di registri dan kembalikan public key-nya.

    Raises:
        VerificationError: bila ``key_id`` tidak dikenal atau sudah dicabut
            (Security v1.1 E5, runbook 8.1).
    """
    entry = registry.get(key_id)
    if entry is None:
        raise VerificationError(f"key_id tidak dikenal: {key_id!r}")
    if entry.revoked:
        raise VerificationError(f"key_id {key_id!r} sudah dicabut")
    return decode_public_key(entry.public_key)


def verify(
    envelope: Envelope,
    registry: Mapping[str, PublicKeyEntry],
    *,
    chain: str = BASE_SEPOLIA,
) -> Payload:
    """Verifikasi envelope dengan public key saja. Mengembalikan payload-nya.

    Urutannya mengikuti PRD v2.1 bagian 9: cari kunci, verifikasi tanda
    tangan, lalu cek nilai ``v``, ``chain_id``, dan ``status``. Nilai diperiksa
    **setelah** tanda tangan supaya alasan penolakannya terpisah, sesuai cek 1
    dan cek 2 di dokumen itu.

    Raises:
        VerificationError: dengan alasan yang bisa dibaca operator dan tidak
            pernah memuat bahan kunci.
    """
    if not isinstance(envelope, Envelope):
        raise VerificationError(f"envelope harus Envelope, bukan {type(envelope).__name__}")

    public_key = resolve_key(registry, envelope.key_id)
    signature = _decode_base64(envelope.signature, "signature")
    if len(signature) != ED25519_SIGNATURE_SIZE:
        raise VerificationError(
            f"tanda tangan harus {ED25519_SIGNATURE_SIZE} byte, dapat {len(signature)} byte"
        )

    try:
        message = signing_bytes(envelope.payload)
    except CanonicalError as exc:
        raise VerificationError(f"payload tidak bisa diserialisasi kanonik: {exc}") from None

    try:
        public_key.verify(signature, message)
    except InvalidSignature:
        raise VerificationError(
            "tanda tangan tidak cocok dengan payload untuk key_id ini"
        ) from None

    payload = envelope.payload
    if payload.v != ENVELOPE_VERSION:
        raise VerificationError(
            f"versi envelope {payload.v!r} tidak dikenal; yang didukung: {ENVELOPE_VERSION!r}"
        )
    expected_chain_id = chain_id_for(chain)
    if payload.chain_id != expected_chain_id:
        raise VerificationError(f"chain_id {payload.chain_id} bukan {expected_chain_id} ({chain})")
    if payload.status != SIGNED_STATUS:
        raise VerificationError(
            f"status {payload.status!r} bukan {SIGNED_STATUS!r}; "
            "hanya keputusan yang disetujui yang ditandatangani"
        )
    return payload


def check_not_expired(payload: Payload, now: int) -> None:
    """Pastikan ``now`` belum melewati ``valid_until``.

    Dipisah dari :func:`verify` dengan sengaja. ``guard verify`` **tidak**
    memanggilnya: ia bekerja tanpa database, dan satu-satunya jam yang
    dipercaya adalah jam database (Arsitektur v1.1 prinsip 5). Executor
    memakainya di cek 3b dengan jam database sebagai ``now``.

    Raises:
        VerificationError: bila sudah kedaluwarsa, atau bila ``now`` tidak
            berupa int positif.
    """
    if isinstance(now, bool) or not isinstance(now, int):
        raise VerificationError(f"now harus int detik UTC, bukan {type(now).__name__}")
    if now <= 0:
        raise VerificationError(f"now harus lebih besar dari nol, dapat {now}")
    if now > payload.valid_until:
        raise VerificationError(
            f"envelope kedaluwarsa: valid_until {payload.valid_until}, now {now}"
        )


# --------------------------------------------------------------------------- #
# Validasi bersama
# --------------------------------------------------------------------------- #


def _require_signable_status(status: object) -> None:
    """Hanya ``approved`` yang boleh ditandatangani (PRD v2 bagian 7)."""
    if status != SIGNED_STATUS:
        raise SigningError(f"hanya status {SIGNED_STATUS!r} yang ditandatangani, bukan {status!r}")


def _require_exact_object(label: str, value: object, types: Mapping[str, type]) -> None:
    """Pastikan ``value`` objek dengan tepat kunci ``types`` dan tipe yang cocok."""
    if not isinstance(value, dict):
        raise EnvelopeError(f"{label} harus objek JSON, bukan {_type_name(value)}")

    present = set(value)
    expected = set(types)
    missing = expected - present
    if missing:
        raise EnvelopeError(f"{label}: field wajib tidak ada: {', '.join(sorted(missing))}")
    unknown = present - expected
    if unknown:
        raise EnvelopeError(f"{label}: field tidak dikenal: {', '.join(sorted(unknown))}")

    for name, expected_type in types.items():
        item = value[name]
        # bool diperiksa lebih dulu: ia subclass int, jadi True akan lolos
        # pemeriksaan int tanpa penjaga ini.
        if isinstance(item, bool):
            raise EnvelopeError(f"{label}.{name}: bool tidak diizinkan di envelope")
        if not isinstance(item, expected_type):
            raise EnvelopeError(
                f"{label}.{name}: harus {expected_type.__name__}, dapat {_type_name(item)}"
            )


def _validate_payload_values(payload: Payload, *, error: type[ValueError]) -> None:
    """Periksa bentuk nilai payload. Tidak memeriksa ``v`` atau ``status``."""
    _require_pattern(error, "recipient", payload.recipient, RECIPIENT_PATTERN, "0x + 40 hex kecil")
    _require_pattern(
        error, "mandate_hash", payload.mandate_hash, MANDATE_HASH_PATTERN, "64 hex kecil"
    )
    _require_pattern(
        error, "decision_id", payload.decision_id, DECISION_ID_PATTERN, "UUID huruf kecil"
    )
    _require_pattern(
        error,
        "amount_base",
        payload.amount_base,
        AMOUNT_BASE_PATTERN,
        "bilangan bulat desimal tanpa leading zero",
    )
    if int(payload.amount_base) <= 0:
        raise error(f"amount_base harus lebih besar dari nol, dapat {payload.amount_base!r}")

    for name in ("mandate_id", "agent_id"):
        value = getattr(payload, name)
        if not value or len(value) > _MAX_OPAQUE_LENGTH:
            raise error(f"{name} harus 1..{_MAX_OPAQUE_LENGTH} karakter")

    for name in ("issued_at", "valid_until"):
        value = getattr(payload, name)
        if value <= 0:
            raise error(f"{name} harus detik UTC positif, dapat {value}")
    if payload.valid_until <= payload.issued_at:
        raise error(
            f"valid_until ({payload.valid_until}) harus setelah issued_at ({payload.issued_at})"
        )


def _require_pattern(
    error: type[ValueError], name: str, value: str, pattern: re.Pattern[str], shape: str
) -> None:
    if pattern.match(value) is None:
        raise error(f"{name} tidak sesuai bentuk ({shape})")


def _require_key_id(key_id: object, *, error: type[ValueError]) -> None:
    if not isinstance(key_id, str) or KEY_ID_PATTERN.match(key_id) is None:
        raise error(
            "key_id tidak sah: huruf kecil, angka, titik, garis bawah, atau "
            "tanda hubung; 1..64 karakter"
        )


def _require_signature_shape(signature: object) -> None:
    if not isinstance(signature, str) or not signature:
        raise EnvelopeError("signature harus string base64 yang tidak kosong")
    if signature.strip() != signature or "\n" in signature:
        raise EnvelopeError("signature tidak boleh memuat spasi atau baris baru")
    _decode_base64(signature, "signature")


def _decode_base64(material: str, label: str) -> bytes:
    """Dekode base64 standar secara ketat. Pesan error tidak memuat isinya."""
    try:
        return base64.b64decode(material, validate=True)
    except (binascii.Error, ValueError):
        raise EnvelopeError(f"{label} bukan base64 standar yang sah") from None


def _type_name(value: object) -> str:
    """Nama tipe JSON untuk pesan error, bukan nama tipe Python."""
    if value is None:
        return "null"
    return {
        bool: "boolean",
        int: "number",
        str: "string",
        list: "array",
        dict: "object",
    }.get(type(value), type(value).__name__)
