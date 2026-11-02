"""Keccak-256.

Keccak-256 (Ethereum) dan SHA3-256 (NIST) memakai permutasi yang sama tapi
padding yang berbeda. Tertukar di antara keduanya adalah kekeliruan yang tidak
kentara: panjang digest sama, keduanya "SHA-3", dan semuanya tetap jalan —
sampai alamat dan tanda tangan ternyata salah. Tes di sini mengunci mana yang
dipakai.
"""

from __future__ import annotations

import hashlib

import pytest

from payment_guard.hashing import keccak256

#: Known answer yang dipakai di mana-mana sebagai penanda Keccak-256 asli.
EMPTY_KECCAK256 = "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"


class TestKnownAnswers:
    def test_empty_input(self) -> None:
        assert keccak256(b"").hex() == EMPTY_KECCAK256

    def test_is_not_sha3_256(self) -> None:
        """Pembeda yang sesungguhnya: padding berbeda, digest berbeda."""
        assert keccak256(b"").hex() != hashlib.sha3_256(b"").hexdigest()

    def test_sha3_256_known_answer_for_contrast(self) -> None:
        """Kalau baris ini yang muncul dari keccak256, backend-nya tertukar."""
        assert (
            hashlib.sha3_256(b"").hexdigest()
            == "a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a"
        )

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            # Vektor yang mudah dicek ulang dengan alat lain.
            (b"abc", "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"),
            (
                b"testing",
                "5f16f4c7f149ac4f9510d9cf8cf384038ad348b3bcdc01915f95de12df9d1b02",
            ),
        ],
    )
    def test_additional_vectors(self, data: bytes, expected: str) -> None:
        assert keccak256(data).hex() == expected


class TestShape:
    def test_digest_is_32_bytes(self) -> None:
        assert len(keccak256(b"apa pun")) == 32

    def test_returns_bytes(self) -> None:
        assert isinstance(keccak256(b""), bytes)

    def test_is_deterministic(self) -> None:
        assert keccak256(b"sama") == keccak256(b"sama")

    def test_distinct_inputs_differ(self) -> None:
        assert keccak256(b"a") != keccak256(b"b")

    def test_accepts_bytearray_and_memoryview(self) -> None:
        reference = keccak256(b"data")
        assert keccak256(bytearray(b"data")) == reference
        assert keccak256(memoryview(b"data")) == reference

    def test_rejects_str(self) -> None:
        """Pengkodean adalah urusan pemanggil, bukan asumsi tersembunyi di sini."""
        with pytest.raises(TypeError, match="butuh bytes"):
            keccak256("bukan bytes")  # type: ignore[arg-type]

    def test_handles_long_input(self) -> None:
        assert len(keccak256(b"x" * 100_000)) == 32
