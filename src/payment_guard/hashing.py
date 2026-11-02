"""Keccak-256, satu-satunya titik sentuh backend hash non-stdlib.

Ethereum memakai **Keccak-256**, yaitu pemenang kontes SHA-3 dengan padding
aslinya. Itu BUKAN SHA3-256 yang distandarkan NIST: keduanya memakai permutasi
Keccak-f[1600] yang sama tapi byte padding yang berbeda, sehingga digest-nya
berbeda untuk input yang sama. :mod:`hashlib` hanya menyediakan yang
distandarkan, jadi backend terpisah diperlukan.

Modul ini adalah **satu-satunya** tempat di ``src/`` yang boleh mengimpor
backend hash pihak ketiga. Dengan begitu, mengganti backend di kemudian hari
tidak menyebar, dan tinjauan manual atas kebenaran hash punya satu berkas
untuk dibaca. Ada tes yang menegakkan pembatasan itu.

Dipakai mulai M1 (normalisasi alamat EIP-55) dan M5 (tanda tangan transaksi).
Dipasang sekarang supaya ia masuk lockfile ber-hash bersama M0.
"""

from __future__ import annotations

from Crypto.Hash import keccak

#: Panjang digest dalam bit. Ethereum memakai 256.
_DIGEST_BITS = 256


def keccak256(data: bytes) -> bytes:
    """Keccak-256 dari ``data``, 32 byte.

    Hanya menerima ``bytes``: pemanggil yang menentukan pengkodean, supaya
    tidak ada asumsi UTF-8 yang tersembunyi di lapisan hash.

    Known answer untuk input kosong:
    ``c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470``.
    Nilai itu berbeda dari ``hashlib.sha3_256(b"").hexdigest()``, dan tes
    memastikan keduanya tidak pernah tertukar.
    """
    if not isinstance(data, bytes | bytearray | memoryview):
        raise TypeError(f"keccak256 butuh bytes, bukan {type(data).__name__}")
    return keccak.new(digest_bits=_DIGEST_BITS, data=bytes(data)).digest()
