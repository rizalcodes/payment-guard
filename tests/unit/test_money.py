"""Parser dan formatter uang. PRD v2.1 bagian 6.2.

Semua penolakan di sini adalah error input (langkah 0 di PRD 6.4), bukan
keputusan: ia tidak pernah tercatat di ``decisions``.
"""

from __future__ import annotations

import pytest

from payment_guard.money import (
    BASE_UNITS_PER_USDC,
    USDC_DECIMALS,
    MoneyError,
    format_amount,
    format_base_units,
    parse_amount,
)


class TestConstants:
    def test_usdc_has_six_decimals(self) -> None:
        assert USDC_DECIMALS == 6
        assert BASE_UNITS_PER_USDC == 1_000_000


class TestValidAmounts:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # Contoh wajib dari spesifikasi.
            ("0.000001", 1),
            ("1", 1_000_000),
            ("25.5", 25_500_000),
            ("50.000000", 50_000_000),
            # Tambahan: pembacaan desimal tidak boleh bergeser.
            ("0.1", 100_000),
            ("0.01", 10_000),
            ("0.000010", 10),
            ("0.100000", 100_000),
            ("1.000001", 1_000_001),
            ("25", 25_000_000),
            ("25.000000", 25_000_000),
            ("999999999", 999_999_999_000_000),
        ],
    )
    def test_parses_to_base_units(self, text: str, expected: int) -> None:
        assert parse_amount(text) == expected

    def test_result_is_int_not_float(self) -> None:
        value = parse_amount("25.5")
        assert isinstance(value, int)
        assert not isinstance(value, float)

    def test_trailing_zeros_do_not_change_value(self) -> None:
        """ "0.1" dan "0.100000" adalah jumlah yang sama."""
        assert parse_amount("0.1") == parse_amount("0.100000")
        assert parse_amount("25") == parse_amount("25.000000")

    def test_smallest_unit_is_accepted(self) -> None:
        assert parse_amount("0.000001") == 1

    def test_very_large_amount_has_no_precision_loss(self) -> None:
        """Di luar jangkauan eksak float64, tapi int tidak peduli."""
        text = "9007199254740993.000001"
        assert parse_amount(text) == 9007199254740993 * BASE_UNITS_PER_USDC + 1


class TestRejectedAmounts:
    @pytest.mark.parametrize(
        ("text", "sebab"),
        [
            ("0.0000001", "lebih dari 6 desimal"),
            ("1.1234567", "lebih dari 6 desimal"),
            ("1e6", "eksponen"),
            ("1E6", "eksponen"),
            ("1.5e3", "eksponen"),
            ("+1", "tanda plus"),
            ("-1", "tanda minus"),
            ("-0.5", "tanda minus"),
            (" 1", "spasi di depan"),
            ("1 ", "spasi di belakang"),
            ("1 000", "spasi di tengah"),
            ("\t1", "tab"),
            ("1\n", "baris baru"),
            ("1,5", "koma desimal"),
            ("1,000", "koma pemisah ribuan"),
            ("", "string kosong"),
            (".5", "tanpa bagian bulat"),
            ("1.", "tanpa bagian desimal"),
            ("007", "leading zero"),
            ("00", "leading zero"),
            ("0007.5", "leading zero"),
            ("NaN", "bukan angka"),
            ("nan", "bukan angka"),
            ("Infinity", "bukan angka"),
            ("inf", "bukan angka"),
            ("-Infinity", "bukan angka"),
            ("0x19", "heksadesimal"),
            ("1_000", "pemisah Python"),
            ("1.2.3", "dua titik"),
            ("25 USDC", "satuan ikut"),
            ("$25", "simbol mata uang"),
            # Digit lebar penuh U+FF12 U+FF15: ambiguitasnya memang yang diuji.
            ("２５", "digit lebar penuh"),  # noqa: RUF001
        ],
    )
    def test_rejected(self, text: str, sebab: str) -> None:
        with pytest.raises(MoneyError):
            parse_amount(text)

    @pytest.mark.parametrize("text", ["0", "0.0", "0.000000", "0.0000"])
    def test_zero_is_rejected(self, text: str) -> None:
        """Bentuknya sah, tapi jumlahnya harus > 0."""
        with pytest.raises(MoneyError, match="lebih besar dari nol"):
            parse_amount(text)

    def test_error_message_names_the_limit(self) -> None:
        with pytest.raises(MoneyError) as exc:
            parse_amount("1.1234567")
        assert "6 desimal" in str(exc.value)

    @pytest.mark.parametrize("value", [1, 1.5, None, True, False, [1], {"a": 1}, b"1"])
    def test_non_string_input_is_rejected(self, value: object) -> None:
        """int ambigu satuannya, float tidak boleh ada di jalur uang."""
        with pytest.raises(MoneyError, match="harus berupa string"):
            parse_amount(value)  # type: ignore[arg-type]

    def test_no_silent_rounding(self) -> None:
        """Yang penting bukan hanya ditolak, tapi juga TIDAK dibulatkan."""
        with pytest.raises(MoneyError):
            parse_amount("0.0000004")
        with pytest.raises(MoneyError):
            parse_amount("0.0000005")
        with pytest.raises(MoneyError):
            parse_amount("0.0000006")


class TestFormatAmount:
    @pytest.mark.parametrize(
        ("base_units", "expected"),
        [
            (1, "0.000001"),
            (10, "0.000010"),
            (100_000, "0.100000"),
            (1_000_000, "1.000000"),
            (25_000_000, "25.000000"),
            (25_500_000, "25.500000"),
            (1_000_001, "1.000001"),
            (0, "0.000000"),
        ],
    )
    def test_formats_with_six_decimals(self, base_units: int, expected: str) -> None:
        assert format_amount(base_units) == expected

    def test_always_has_six_decimals(self) -> None:
        for base_units in (1, 1_000_000, 123_456_789):
            whole, _, fraction = format_amount(base_units).partition(".")
            assert whole
            assert len(fraction) == USDC_DECIMALS

    @pytest.mark.parametrize("value", [1.0, "1", None, True])
    def test_non_int_is_rejected(self, value: object) -> None:
        with pytest.raises(MoneyError, match="harus int"):
            format_amount(value)  # type: ignore[arg-type]

    def test_negative_is_rejected(self) -> None:
        with pytest.raises(MoneyError, match="negatif"):
            format_amount(-1)


class TestFormatBaseUnits:
    @pytest.mark.parametrize(
        ("base_units", "expected"),
        [(1, "1"), (25_000_000, "25000000"), (0, "0")],
    )
    def test_formats_as_integer_string(self, base_units: int, expected: str) -> None:
        assert format_base_units(base_units) == expected

    def test_has_no_separators_or_decimal_point(self) -> None:
        text = format_base_units(1_234_567_890)
        assert text == "1234567890"
        assert "." not in text
        assert "_" not in text
        assert "," not in text

    @pytest.mark.parametrize("value", [1.0, "1", None, True])
    def test_non_int_is_rejected(self, value: object) -> None:
        with pytest.raises(MoneyError, match="harus int"):
            format_base_units(value)  # type: ignore[arg-type]

    def test_negative_is_rejected(self) -> None:
        with pytest.raises(MoneyError, match="negatif"):
            format_base_units(-1)


class TestRoundTrip:
    @pytest.mark.parametrize(
        "base_units",
        [
            1,
            10,
            999,
            1_000_000,
            25_500_000,
            50_000_000,
            1_000_001,
            123_456_789_012,
            9_007_199_254_740_993_000_001,
        ],
    )
    def test_parse_of_format_returns_the_same_int(self, base_units: int) -> None:
        assert parse_amount(format_amount(base_units)) == base_units

    @pytest.mark.parametrize("text", ["0.000001", "1", "25.5", "50.000000", "0.1", "123456.654321"])
    def test_format_of_parse_is_the_normalised_form(self, text: str) -> None:
        """Bentuk ternormalisasi stabil: format(parse(x)) idempoten."""
        once = format_amount(parse_amount(text))
        twice = format_amount(parse_amount(once))
        assert once == twice
        assert parse_amount(once) == parse_amount(text)
