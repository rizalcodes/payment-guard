"""Serialisasi JSON kanonik.

Sifat yang menentukan: satu makna menghasilkan satu rangkaian byte. Kalau itu
pernah gagal, ``mandate_hash`` dan tanda tangan M2 akan berubah tanpa ada
perubahan makna.
"""

from __future__ import annotations

import json

import pytest

from payment_guard.canonical import (
    ALLOWED_TYPES,
    CanonicalError,
    canonical_bytes,
    canonical_text,
    validate,
)


class TestKeyOrderDoesNotMatter:
    def test_different_insertion_order_gives_identical_bytes(self) -> None:
        first = {"a": "1", "b": "2", "c": "3"}
        second = {"c": "3", "a": "1", "b": "2"}
        third = {"b": "2", "c": "3", "a": "1"}
        assert canonical_bytes(first) == canonical_bytes(second) == canonical_bytes(third)

    def test_nested_order_also_does_not_matter(self) -> None:
        first = {"outer": {"z": "1", "a": "2"}, "other": ["x", "y"]}
        second = {"other": ["x", "y"], "outer": {"a": "2", "z": "1"}}
        assert canonical_bytes(first) == canonical_bytes(second)

    def test_keys_are_sorted_in_output(self) -> None:
        assert canonical_text({"c": "3", "a": "1", "b": "2"}) == '{"a":"1","b":"2","c":"3"}'

    def test_list_order_is_preserved(self) -> None:
        """Urutan kunci tidak bermakna; urutan elemen list bermakna."""
        assert canonical_bytes(["a", "b"]) != canonical_bytes(["b", "a"])


class TestNoWhitespace:
    def test_no_spaces_after_separators(self) -> None:
        text = canonical_text({"a": "1", "b": ["x", "y"], "c": {"d": "2"}})
        assert text == '{"a":"1","b":["x","y"],"c":{"d":"2"}}'
        assert " " not in text

    def test_no_trailing_newline(self) -> None:
        assert not canonical_bytes({"a": "1"}).endswith(b"\n")


class TestEncoding:
    def test_utf8_without_ascii_escaping(self) -> None:
        """Non-ASCII ditulis apa adanya dalam UTF-8, bukan di-escape \\uXXXX."""
        raw = canonical_bytes({"nama": "biaya kopi é中"})
        assert raw == '{"nama":"biaya kopi é中"}'.encode()
        assert b"\\u" not in raw

    def test_output_is_bytes(self) -> None:
        assert isinstance(canonical_bytes({"a": "1"}), bytes)

    def test_round_trips_through_json(self) -> None:
        value = {"a": "1", "b": [1, 2, 3], "c": {"d": "x"}}
        assert json.loads(canonical_bytes(value).decode("utf-8")) == value


class TestAllowedTypes:
    def test_allowed_set_is_exactly_four(self) -> None:
        assert ALLOWED_TYPES == (dict, list, str, int)

    @pytest.mark.parametrize(
        "value",
        [
            {"a": "teks"},
            {"a": 0},
            {"a": 1},
            {"a": -1},
            {"a": 10**30},
            {"a": []},
            {"a": {}},
            {"a": ["x", 1, {"b": "y"}]},
            [],
            {},
            "teks",
            0,
        ],
    )
    def test_accepted(self, value: object) -> None:
        canonical_bytes(value)

    def test_large_int_is_not_degraded(self) -> None:
        """Base units bisa besar; tidak boleh jadi float di mana pun."""
        value = 9_007_199_254_740_993_000_001
        assert canonical_text({"amount": value}) == f'{{"amount":{value}}}'


class TestFloatIsRejected:
    @pytest.mark.parametrize("value", [1.0, 0.1, -2.5, 1e10, float("inf"), float("nan")])
    def test_float_value_rejected(self, value: float) -> None:
        with pytest.raises(CanonicalError, match="float tidak diizinkan"):
            canonical_bytes({"amount": value})

    def test_float_nested_deeply_is_rejected(self) -> None:
        with pytest.raises(CanonicalError, match="float"):
            canonical_bytes({"a": {"b": [{"c": 1.5}]}})

    def test_error_message_names_the_path(self) -> None:
        with pytest.raises(CanonicalError) as exc:
            canonical_bytes({"limits": {"max_per_tx": 1.0}})
        assert "$.limits.max_per_tx" in str(exc.value)

    def test_list_index_appears_in_the_path(self) -> None:
        with pytest.raises(CanonicalError) as exc:
            canonical_bytes({"allowlist": ["0x1", 2.0]})
        assert "$.allowlist[1]" in str(exc.value)


class TestBoolIsRejected:
    @pytest.mark.parametrize("value", [True, False])
    def test_bool_rejected(self, value: bool) -> None:
        """bool adalah subclass int di Python, jadi mudah lolos tanpa sengaja."""
        with pytest.raises(CanonicalError, match="bool tidak diizinkan"):
            canonical_bytes({"aktif": value})

    def test_bool_in_list_rejected(self) -> None:
        with pytest.raises(CanonicalError, match="bool"):
            canonical_bytes(["x", True])

    def test_int_still_accepted_after_the_bool_guard(self) -> None:
        """Penjaga bool tidak boleh ikut menolak int biasa."""
        assert canonical_text({"n": 1}) == '{"n":1}'
        assert canonical_text({"n": 0}) == '{"n":0}'


class TestNoneIsRejected:
    def test_none_value_rejected(self) -> None:
        with pytest.raises(CanonicalError, match="None tidak diizinkan"):
            canonical_bytes({"a": None})

    def test_none_as_whole_value_rejected(self) -> None:
        with pytest.raises(CanonicalError, match="None"):
            canonical_bytes(None)

    def test_message_suggests_omitting_the_field(self) -> None:
        with pytest.raises(CanonicalError) as exc:
            canonical_bytes({"a": None})
        assert "hilangkan" in str(exc.value)


class TestOtherTypesRejected:
    @pytest.mark.parametrize(
        "value",
        [
            {"a": b"bytes"},
            {"a": (1, 2)},
            {"a": {1, 2}},
            {"a": object()},
            {"a": complex(1, 2)},
        ],
    )
    def test_rejected(self, value: object) -> None:
        with pytest.raises(CanonicalError, match="tidak diizinkan"):
            canonical_bytes(value)

    def test_decimal_is_rejected(self) -> None:
        """Decimal eksak, tapi serialisasinya ambigu. Pakai int base units."""
        from decimal import Decimal

        with pytest.raises(CanonicalError):
            canonical_bytes({"a": Decimal("1.5")})

    def test_non_string_key_is_rejected(self) -> None:
        with pytest.raises(CanonicalError, match="kunci objek harus string"):
            canonical_bytes({1: "a"})

    def test_bool_key_is_rejected(self) -> None:
        with pytest.raises(CanonicalError, match="kunci objek"):
            canonical_bytes({True: "a"})


class TestValidateIsSeparatelyUsable:
    def test_validate_returns_none_for_valid(self) -> None:
        assert validate({"a": "1"}) is None

    def test_validate_raises_for_invalid(self) -> None:
        with pytest.raises(CanonicalError):
            validate({"a": 1.0})

    def test_validate_does_not_serialise(self) -> None:
        """validate() murni pemeriksaan; pemanggil yang memutuskan serialisasi."""
        value: dict[str, object] = {"a": "1"}
        validate(value)
        assert value == {"a": "1"}


class TestDeterminism:
    def test_same_input_gives_same_bytes_every_time(self) -> None:
        value = {"b": "2", "a": ["x", 1], "c": {"d": "y"}}
        results = {canonical_bytes(value) for _ in range(20)}
        assert len(results) == 1

    def test_equal_values_built_differently_give_same_bytes(self) -> None:
        built = {}
        built["b"] = "2"
        built["a"] = "1"
        literal = {"a": "1", "b": "2"}
        assert canonical_bytes(built) == canonical_bytes(literal)
