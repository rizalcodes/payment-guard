"""Alamat dan checksum EIP-55.

Empat vektor resmi dari EIP-55 dipakai sebagai known answer. Kalau backend
hash tertukar menjadi SHA3-256, keempatnya akan gagal sekaligus.
"""

from __future__ import annotations

import pytest

from payment_guard.addresses import (
    AddressError,
    has_single_case,
    is_valid_address,
    normalize_address,
    normalize_allowlist,
    to_checksum_address,
)

#: Vektor resmi EIP-55 (bagian "Test Cases", alamat semua-kapital dan
#: semua-kecil).
EIP55_VECTORS = (
    "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
    "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
    "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
    "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
)


def flip_one_letter_case(address: str) -> str:
    """Balik kasus SATU huruf, tapi jaga alamatnya tetap campuran huruf.

    Kalau hasilnya jadi satu kasus, ia akan diterima tanpa checksum dan tesnya
    jadi tidak berarti. Huruf yang dibalik dipilih supaya itu tidak terjadi.
    """
    body = address[2:]
    for index, character in enumerate(body):
        if not character.isalpha():
            continue
        flipped = character.lower() if character.isupper() else character.upper()
        candidate = "0x" + body[:index] + flipped + body[index + 1 :]
        if not has_single_case(candidate):
            return candidate
    raise AssertionError(f"tidak bisa membalik satu huruf pada {address}")


class TestChecksumVectors:
    @pytest.mark.parametrize("address", EIP55_VECTORS)
    def test_official_vector_round_trips(self, address: str) -> None:
        """to_checksum_address harus mereproduksi vektor resmi persis."""
        assert to_checksum_address(address) == address

    @pytest.mark.parametrize("address", EIP55_VECTORS)
    def test_official_vector_is_accepted(self, address: str) -> None:
        assert normalize_address(address) == address.lower()

    @pytest.mark.parametrize("address", EIP55_VECTORS)
    def test_checksum_is_derived_from_the_hex_value_only(self, address: str) -> None:
        """Kasus huruf masukan tidak berpengaruh pada hasil checksum."""
        body = address[2:]
        assert to_checksum_address("0x" + body.lower()) == address
        assert to_checksum_address("0x" + body.upper()) == address

    def test_vectors_are_genuinely_mixed_case(self) -> None:
        """Penjaga: kalau vektornya satu kasus, tes checksum jadi hampa."""
        for address in EIP55_VECTORS:
            assert not has_single_case(address), address


class TestWrongChecksumIsRejected:
    @pytest.mark.parametrize("address", EIP55_VECTORS)
    def test_one_flipped_letter_is_rejected(self, address: str) -> None:
        broken = flip_one_letter_case(address)
        assert broken != address
        assert not has_single_case(broken)
        with pytest.raises(AddressError, match="checksum"):
            normalize_address(broken)

    def test_rejection_message_shows_the_correct_form(self) -> None:
        broken = flip_one_letter_case(EIP55_VECTORS[0])
        with pytest.raises(AddressError) as exc:
            normalize_address(broken)
        assert EIP55_VECTORS[0] in str(exc.value)

    def test_all_vectors_reject_each_others_case_pattern(self) -> None:
        """Nilai hex benar, pola kapitalisasi dari alamat lain -> ditolak."""
        first_body = EIP55_VECTORS[0][2:]
        second_pattern = EIP55_VECTORS[1][2:]
        mixed = "".join(
            character.upper() if pattern.isupper() else character.lower()
            for character, pattern in zip(first_body, second_pattern, strict=True)
        )
        candidate = "0x" + mixed
        if candidate != EIP55_VECTORS[0] and not has_single_case(candidate):
            with pytest.raises(AddressError):
                normalize_address(candidate)


class TestSingleCaseIsAcceptedWithoutChecksum:
    @pytest.mark.parametrize("address", EIP55_VECTORS)
    def test_lowercase_is_accepted(self, address: str) -> None:
        lower = address.lower()
        assert normalize_address(lower) == lower

    @pytest.mark.parametrize("address", EIP55_VECTORS)
    def test_uppercase_is_accepted(self, address: str) -> None:
        upper = "0x" + address[2:].upper()
        assert normalize_address(upper) == address.lower()

    @pytest.mark.parametrize("address", EIP55_VECTORS)
    def test_both_cases_normalise_to_the_same_value(self, address: str) -> None:
        lower = normalize_address(address.lower())
        upper = normalize_address("0x" + address[2:].upper())
        checksummed = normalize_address(address)
        assert lower == upper == checksummed

    def test_digits_only_address_is_accepted(self) -> None:
        address = "0x" + "1234567890" * 4
        assert has_single_case(address)
        assert normalize_address(address) == address

    def test_prefix_0x_is_not_case_folded(self) -> None:
        """Awalan harus '0x' huruf kecil; '0X' tidak diterima."""
        with pytest.raises(AddressError):
            normalize_address("0X" + EIP55_VECTORS[0][2:].lower())


class TestShape:
    @pytest.mark.parametrize(
        ("address", "sebab"),
        [
            ("", "kosong"),
            ("0x", "tanpa isi"),
            ("5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed", "tanpa awalan 0x"),
            ("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAe", "39 karakter"),
            ("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAedd", "41 karakter"),
            ("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeg", "karakter non-hex"),
            ("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeA d", "spasi di tengah"),
            (" 0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed", "spasi di depan"),
            ("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed ", "spasi di belakang"),
            ("0x" + "0" * 39 + "é", "non-ASCII"),
        ],
    )
    def test_bad_shape_is_rejected(self, address: str, sebab: str) -> None:
        with pytest.raises(AddressError):
            normalize_address(address)

    @pytest.mark.parametrize("value", [None, 1, True, b"0x" + b"0" * 40, ["0x0"]])
    def test_non_string_is_rejected(self, value: object) -> None:
        with pytest.raises(AddressError, match="harus berupa string"):
            normalize_address(value)  # type: ignore[arg-type]

    def test_zero_address_is_shape_valid(self) -> None:
        """Alamat nol sah bentuknya. Penolakannya urusan allowlist, bukan sini."""
        zero = "0x" + "0" * 40
        assert normalize_address(zero) == zero


class TestIsValidAddress:
    @pytest.mark.parametrize("address", EIP55_VECTORS)
    def test_true_for_valid(self, address: str) -> None:
        assert is_valid_address(address) is True
        assert is_valid_address(address.lower()) is True

    def test_false_for_bad_checksum(self) -> None:
        assert is_valid_address(flip_one_letter_case(EIP55_VECTORS[0])) is False

    def test_false_for_bad_shape(self) -> None:
        assert is_valid_address("0x123") is False

    def test_does_not_raise(self) -> None:
        assert is_valid_address(None) is False  # type: ignore[arg-type]


class TestNormalizeAllowlist:
    def test_sorted_and_lowercased(self) -> None:
        result = normalize_allowlist(list(EIP55_VECTORS))
        assert result == sorted(address.lower() for address in EIP55_VECTORS)

    def test_input_order_does_not_matter(self) -> None:
        """Dasar dari ST-07: urutan berbeda harus memberi bentuk yang sama."""
        forward = normalize_allowlist(list(EIP55_VECTORS))
        backward = normalize_allowlist(list(reversed(EIP55_VECTORS)))
        assert forward == backward

    def test_mixed_input_cases_give_the_same_result(self) -> None:
        checksummed = normalize_allowlist(list(EIP55_VECTORS))
        lowered = normalize_allowlist([a.lower() for a in EIP55_VECTORS])
        uppered = normalize_allowlist(["0x" + a[2:].upper() for a in EIP55_VECTORS])
        assert checksummed == lowered == uppered

    def test_duplicates_are_removed(self) -> None:
        address = EIP55_VECTORS[0]
        result = normalize_allowlist([address, address.lower(), "0x" + address[2:].upper()])
        assert result == [address.lower()]

    def test_one_bad_address_rejects_the_whole_list(self) -> None:
        with pytest.raises(AddressError):
            normalize_allowlist([EIP55_VECTORS[0], "0x123"])

    def test_bad_checksum_in_list_is_rejected(self) -> None:
        with pytest.raises(AddressError, match="checksum"):
            normalize_allowlist([EIP55_VECTORS[0], flip_one_letter_case(EIP55_VECTORS[1])])

    def test_empty_list_is_rejected(self) -> None:
        with pytest.raises(AddressError, match="tidak boleh kosong"):
            normalize_allowlist([])

    @pytest.mark.parametrize("value", [None, "0x0", {"a": 1}])
    def test_non_list_is_rejected(self, value: object) -> None:
        with pytest.raises(AddressError, match="harus berupa list"):
            normalize_allowlist(value)  # type: ignore[arg-type]


class TestKeccakNotSha3:
    """Penjaga eksplisit: backend hash tidak boleh tertukar.

    Checksum EIP-55 dihitung dari Keccak-256. Kalau seseorang menukarnya
    dengan ``hashlib.sha3_256``, keempat vektor resmi akan gagal. Tes ini
    menyatakan kaitannya supaya kegagalan itu terbaca sebagai "backend salah",
    bukan "vektor salah".
    """

    def test_checksum_uses_keccak256(self) -> None:
        import hashlib

        from payment_guard.hashing import keccak256

        body = EIP55_VECTORS[0][2:].lower()
        keccak_digest = keccak256(body.encode("ascii")).hex()
        sha3_digest = hashlib.sha3_256(body.encode("ascii")).hexdigest()
        assert keccak_digest != sha3_digest

        # Checksum yang benar konsisten dengan digest Keccak, bukan SHA3.
        expected = to_checksum_address(EIP55_VECTORS[0])
        for index, character in enumerate(body):
            if character.isalpha():
                should_be_upper = int(keccak_digest[index], 16) >= 8
                assert expected[2 + index].isupper() == should_be_upper
