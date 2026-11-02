"""Alamat Ethereum: validasi, checksum EIP-55, normalisasi ke lowercase.

Aturan yang dipakai (PRD v2.1 bagian 6.1, Arsitektur v1.1 bagian 5.1):

* Bentuk wajib ``0x`` + 40 hex.
* Alamat **satu kasus** (semua huruf kecil, atau semua huruf besar) diterima
  apa adanya: tidak ada informasi checksum di dalamnya untuk diperiksa.
* Alamat **campuran huruf** WAJIB lolos checksum EIP-55. Campuran huruf adalah
  klaim: ia menyatakan "saya sudah di-checksum". Klaim itu diverifikasi, karena
  di situlah salah ketik satu karakter tertangkap.
* Bentuk ternormalisasi selalu lowercase, cocok dengan CHECK di
  ``0001_init.sql`` (``^0x[0-9a-f]{40}$``).

Checksum memakai :func:`payment_guard.hashing.keccak256`. **Bukan**
``hashlib.sha3_256``: EIP-55 memakai Keccak-256 dengan padding aslinya, dan
SHA3-256 akan memberi hasil berbeda sehingga setiap alamat ber-checksum sah
akan ditolak.

Modul ini murni: tidak menyentuh database, jaringan, atau jam.
"""

from __future__ import annotations

import re
from typing import Final

from payment_guard.hashing import keccak256

#: Bentuk alamat yang diterima sebelum checksum diperiksa.
ADDRESS_PATTERN: Final = re.compile(r"^0x[0-9a-fA-F]{40}$")

#: Panjang bagian hex, tanpa awalan ``0x``.
ADDRESS_HEX_LENGTH: Final = 40

#: Nibble hash >= 8 berarti huruf di posisi itu ditulis kapital (EIP-55).
_UPPERCASE_THRESHOLD: Final = 8


class AddressError(ValueError):
    """Alamat tidak sah. Error input, bukan keputusan."""


def to_checksum_address(address: str) -> str:
    """Kembalikan bentuk ber-checksum EIP-55 dari ``address``.

    Kasus huruf masukan diabaikan: hanya nilai hex-nya yang dipakai.

    Raises:
        AddressError: bila bentuknya bukan ``0x`` + 40 hex.
    """
    body = _require_shape(address)
    lower = body.lower()
    digest = keccak256(lower.encode("ascii")).hex()

    out = []
    for index, character in enumerate(lower):
        if character.isdigit():
            out.append(character)
        elif int(digest[index], 16) >= _UPPERCASE_THRESHOLD:
            out.append(character.upper())
        else:
            out.append(character)
    return "0x" + "".join(out)


def has_single_case(address: str) -> bool:
    """True bila huruf a-f pada ``address`` seluruhnya kecil atau seluruhnya besar.

    Alamat seperti itu tidak membawa klaim checksum, jadi tidak ada yang bisa
    diverifikasi. Alamat yang seluruhnya digit juga termasuk di sini.
    """
    body = _require_shape(address)
    letters = [character for character in body if character.isalpha()]
    return (
        not letters
        or all(character.islower() for character in letters)
        or all(character.isupper() for character in letters)
    )


def is_valid_address(address: str) -> bool:
    """True bila ``address`` diterima oleh :func:`normalize_address`."""
    try:
        normalize_address(address)
    except AddressError:
        return False
    return True


def normalize_address(address: str) -> str:
    """Validasi ``address`` lalu kembalikan bentuk lowercase-nya.

    Alamat campuran huruf diverifikasi terhadap checksum EIP-55 dan ditolak
    bila tidak cocok. Alamat satu kasus diterima tanpa pemeriksaan checksum.

    Raises:
        AddressError: bila bentuknya salah atau checksum-nya tidak cocok.
    """
    body = _require_shape(address)
    if has_single_case(address):
        return "0x" + body.lower()

    expected = to_checksum_address(address)
    if address != expected:
        raise AddressError(
            f"alamat {address} campuran huruf besar dan kecil, jadi ia mengaku "
            "sudah ber-checksum EIP-55, tapi checksum-nya tidak cocok. "
            f"Bentuk yang benar untuk nilai hex itu: {expected}"
        )
    return "0x" + body.lower()


def normalize_allowlist(addresses: list[str]) -> list[str]:
    """Normalisasi dan urutkan daftar alamat, tanpa duplikat.

    Pengurutan dan deduplikasi membuat dua allowlist dengan makna sama
    menghasilkan byte kanonik yang sama (Arsitektur v1.1 bagian 5.1).

    Raises:
        AddressError: bila ada alamat yang tidak sah, atau daftarnya kosong.
    """
    if not isinstance(addresses, list):
        raise AddressError(f"allowlist harus berupa list, bukan {type(addresses).__name__}")
    if not addresses:
        raise AddressError("allowlist tidak boleh kosong")

    normalized = {normalize_address(address) for address in addresses}
    return sorted(normalized)


def _require_shape(address: str) -> str:
    """Pastikan bentuk ``0x`` + 40 hex. Kembalikan bagian hex-nya."""
    if isinstance(address, bool) or not isinstance(address, str):
        raise AddressError(f"alamat harus berupa string, bukan {type(address).__name__}")
    if ADDRESS_PATTERN.match(address) is None:
        raise AddressError(
            f"alamat {address!r} tidak sah: harus '0x' diikuti "
            f"{ADDRESS_HEX_LENGTH} karakter heksadesimal"
        )
    return address[2:]
