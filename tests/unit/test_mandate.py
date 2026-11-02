"""Mandate: validasi, normalisasi, dan hash. ST-07 dan ST-32."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, ClassVar

import pytest

from payment_guard.canonical import canonical_bytes
from payment_guard.mandate import (
    MANDATE_HASH_PREFIX,
    Mandate,
    MandateError,
    compute_mandate_hash,
    load_mandate,
    normalized_bytes,
    normalized_form,
)

VALID: dict[str, Any] = {
    "mandate_id": "demo-2026-11",
    "agent_id": "demo-agent",
    "chain": "base-sepolia",
    "allowed_recipients": [
        "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
        "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
    ],
    "max_per_tx": "25",
    "max_per_day": "100",
    "human_approval_above": "25",
    "retry_cooldown_seconds": 3600,
    "aggregation_window_seconds": 3600,
    "decision_ttl_seconds": 900,
    "human_wait_ttl_seconds": 900,
}


def mandate_with(**overrides: Any) -> dict[str, Any]:
    data = copy.deepcopy(VALID)
    data.update(overrides)
    return data


def hash_of(**overrides: Any) -> str:
    return compute_mandate_hash(load_mandate(mandate_with(**overrides)))


class TestHappyPath:
    def test_loads_and_normalises(self) -> None:
        mandate = load_mandate(VALID)
        assert mandate.mandate_id == "demo-2026-11"
        assert mandate.agent_id == "demo-agent"
        assert mandate.max_per_tx == 25_000_000
        assert mandate.max_per_day == 100_000_000
        assert mandate.human_approval_above == 25_000_000
        assert mandate.retry_cooldown_seconds == 3600

    def test_allowlist_is_lowercase_sorted_tuple(self) -> None:
        mandate = load_mandate(VALID)
        assert mandate.allowed_recipients == tuple(
            sorted(a.lower() for a in VALID["allowed_recipients"])
        )
        assert isinstance(mandate.allowed_recipients, tuple)

    def test_amounts_are_int_base_units(self) -> None:
        mandate = load_mandate(VALID)
        for value in (mandate.max_per_tx, mandate.max_per_day, mandate.human_approval_above):
            assert isinstance(value, int)
            assert not isinstance(value, float)

    def test_mandate_is_frozen(self) -> None:
        mandate = load_mandate(VALID)
        with pytest.raises(AttributeError):
            mandate.max_per_tx = 1  # type: ignore[misc]

    def test_allows_recipient(self) -> None:
        mandate = load_mandate(VALID)
        assert mandate.allows_recipient(VALID["allowed_recipients"][0].lower())
        assert not mandate.allows_recipient("0x" + "9" * 40)

    def test_input_mapping_is_not_mutated(self) -> None:
        before = copy.deepcopy(VALID)
        load_mandate(VALID)
        assert VALID == before


class TestAgentId:
    """ST-32: agent_id dengan ':', spasi, huruf besar, atau >64 karakter ditolak."""

    @pytest.mark.parametrize(
        ("agent_id", "sebab"),
        [
            ("demo:agent", "titik dua"),
            ("demo agent", "spasi"),
            ("DemoAgent", "huruf besar"),
            ("DEMO", "semua huruf besar"),
            ("a" * 65, "lebih dari 64 karakter"),
            ("", "kosong"),
            ("-demo", "diawali tanda hubung"),
            ("_demo", "diawali garis bawah"),
            ("demo.agent", "titik"),
            ("demo/agent", "garis miring"),
            ("demo@agent", "at"),
            ("demo\nagent", "baris baru di tengah"),
            ("demo-agent\n", "baris baru di akhir"),
            (" demo", "spasi di depan"),
            ("demo ", "spasi di belakang"),
            ("demö", "non-ASCII"),
        ],
    )
    def test_rejected(self, agent_id: str, sebab: str) -> None:
        with pytest.raises(MandateError, match="agent_id"):
            load_mandate(mandate_with(agent_id=agent_id))

    @pytest.mark.parametrize(
        "agent_id",
        ["a", "0", "demo-agent", "demo_agent", "agent9", "a" * 64, "0abc-def_9"],
    )
    def test_accepted(self, agent_id: str) -> None:
        assert load_mandate(mandate_with(agent_id=agent_id)).agent_id == agent_id

    def test_boundary_is_exactly_64(self) -> None:
        assert load_mandate(mandate_with(agent_id="a" * 64)).agent_id == "a" * 64
        with pytest.raises(MandateError):
            load_mandate(mandate_with(agent_id="a" * 65))

    @pytest.mark.parametrize("value", [None, 1, True, ["a"]])
    def test_non_string_rejected(self, value: object) -> None:
        with pytest.raises(MandateError, match="agent_id harus berupa string"):
            load_mandate(mandate_with(agent_id=value))


class TestMandateId:
    @pytest.mark.parametrize("mandate_id", ["a", "demo-2026-11", "x" * 64])
    def test_accepted(self, mandate_id: str) -> None:
        assert load_mandate(mandate_with(mandate_id=mandate_id)).mandate_id == mandate_id

    @pytest.mark.parametrize(
        ("mandate_id", "sebab"),
        [("", "kosong"), ("x" * 65, "lebih dari 64"), ("a b", "spasi"), ("a\nb", "kontrol")],
    )
    def test_rejected(self, mandate_id: str, sebab: str) -> None:
        with pytest.raises(MandateError, match="mandate_id"):
            load_mandate(mandate_with(mandate_id=mandate_id))

    def test_length_bounds_match_the_sql_check(self) -> None:
        """0001_init.sql: char_length(mandate_id) BETWEEN 1 AND 64."""
        load_mandate(mandate_with(mandate_id="x"))
        load_mandate(mandate_with(mandate_id="x" * 64))
        with pytest.raises(MandateError):
            load_mandate(mandate_with(mandate_id="x" * 65))


class TestAllowlist:
    def test_checksum_is_enforced(self) -> None:
        broken = "0x5aaeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
        with pytest.raises(MandateError, match="allowed_recipients"):
            load_mandate(mandate_with(allowed_recipients=[broken]))

    def test_bad_shape_rejected(self) -> None:
        with pytest.raises(MandateError, match="allowed_recipients"):
            load_mandate(mandate_with(allowed_recipients=["0x123"]))

    def test_empty_rejected(self) -> None:
        with pytest.raises(MandateError, match="allowed_recipients"):
            load_mandate(mandate_with(allowed_recipients=[]))

    @pytest.mark.parametrize("value", [None, "0x" + "a" * 40, {"a": 1}, 1])
    def test_non_list_rejected(self, value: object) -> None:
        with pytest.raises(MandateError, match="allowed_recipients"):
            load_mandate(mandate_with(allowed_recipients=value))

    def test_duplicates_collapse(self) -> None:
        address = VALID["allowed_recipients"][0]
        mandate = load_mandate(
            mandate_with(allowed_recipients=[address, address.lower(), "0x" + address[2:].upper()])
        )
        assert mandate.allowed_recipients == (address.lower(),)


class TestMoneyFields:
    @pytest.mark.parametrize("field", ["max_per_tx", "max_per_day", "human_approval_above"])
    def test_uses_the_money_parser(self, field: str) -> None:
        with pytest.raises(MandateError, match=field):
            load_mandate(mandate_with(**{field: "1.1234567"}))

    @pytest.mark.parametrize("field", ["max_per_tx", "max_per_day", "human_approval_above"])
    def test_zero_rejected(self, field: str) -> None:
        with pytest.raises(MandateError, match=field):
            load_mandate(mandate_with(**{field: "0"}))

    @pytest.mark.parametrize("value", [25, 25.0, None, True])
    def test_non_string_amount_rejected(self, value: object) -> None:
        """Angka telanjang ambigu satuannya; float dilarang sama sekali."""
        with pytest.raises(MandateError, match="max_per_tx"):
            load_mandate(mandate_with(max_per_tx=value))


class TestSecondsFields:
    @pytest.mark.parametrize(
        "field", ["retry_cooldown_seconds", "human_wait_ttl_seconds", "decision_ttl_seconds"]
    )
    def test_must_be_positive_int(self, field: str) -> None:
        with pytest.raises(MandateError, match=field):
            load_mandate(mandate_with(**{field: 0}))
        with pytest.raises(MandateError, match=field):
            load_mandate(mandate_with(**{field: -1}))

    @pytest.mark.parametrize(
        "field", ["retry_cooldown_seconds", "human_wait_ttl_seconds", "decision_ttl_seconds"]
    )
    @pytest.mark.parametrize("value", ["3600", 3600.0, None, True])
    def test_non_int_rejected(self, field: str, value: object) -> None:
        with pytest.raises(MandateError, match=field):
            load_mandate(mandate_with(**{field: value}))

    def test_one_second_is_accepted(self) -> None:
        assert load_mandate(mandate_with(retry_cooldown_seconds=1)).retry_cooldown_seconds == 1


class TestLimitOrdering:
    def test_human_approval_above_may_equal_max_per_tx(self) -> None:
        load_mandate(mandate_with(human_approval_above="25", max_per_tx="25"))

    def test_max_per_tx_may_equal_max_per_day(self) -> None:
        load_mandate(mandate_with(max_per_tx="100", max_per_day="100"))

    def test_human_approval_above_cannot_exceed_max_per_tx(self) -> None:
        with pytest.raises(MandateError, match="human_approval_above"):
            load_mandate(mandate_with(human_approval_above="30", max_per_tx="25"))

    def test_max_per_tx_cannot_exceed_max_per_day(self) -> None:
        with pytest.raises(MandateError, match="max_per_tx"):
            load_mandate(mandate_with(max_per_tx="200", max_per_day="100"))

    def test_message_explains_why_it_matters(self) -> None:
        with pytest.raises(MandateError) as exc:
            load_mandate(mandate_with(human_approval_above="30", max_per_tx="25"))
        assert "persetujuan manusia" in str(exc.value)


class TestStrictFields:
    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(MandateError, match="field tidak dikenal: catatan"):
            load_mandate(mandate_with(catatan="apa saja"))

    def test_rejection_explains_the_hash_risk(self) -> None:
        with pytest.raises(MandateError) as exc:
            load_mandate(mandate_with(catatan="x"))
        assert "hash sama" in str(exc.value)

    @pytest.mark.parametrize("field", sorted(VALID))
    def test_missing_field_rejected(self, field: str) -> None:
        data = copy.deepcopy(VALID)
        del data[field]
        with pytest.raises(MandateError, match=f"field wajib tidak ada.*{field}"):
            load_mandate(data)

    @pytest.mark.parametrize("value", [None, [], "teks", 1])
    def test_non_mapping_rejected(self, value: object) -> None:
        with pytest.raises(MandateError, match="harus berupa objek"):
            load_mandate(value)  # type: ignore[arg-type]


class TestErrorMessagesAreSafe:
    def test_message_names_the_field(self) -> None:
        with pytest.raises(MandateError) as exc:
            load_mandate(mandate_with(max_per_day="tidak-sah"))
        assert "max_per_day" in str(exc.value)

    def test_long_value_is_truncated(self) -> None:
        with pytest.raises(MandateError) as exc:
            load_mandate(mandate_with(agent_id="A" * 500))
        message = str(exc.value)
        assert "dipotong" in message
        assert len(message) < 400

    def test_message_does_not_dump_the_whole_mandate(self) -> None:
        with pytest.raises(MandateError) as exc:
            load_mandate(mandate_with(agent_id="BAD"))
        message = str(exc.value)
        assert "allowed_recipients" not in message
        assert "retry_cooldown_seconds" not in message


class TestNormalisedForm:
    def test_only_canonical_types(self) -> None:
        """Bentuk ternormalisasi harus diterima canonical_bytes apa adanya."""
        canonical_bytes(normalized_form(load_mandate(VALID)))

    def test_money_is_base_unit_strings(self) -> None:
        form = normalized_form(load_mandate(VALID))
        assert form["max_per_tx"] == "25000000"
        assert form["max_per_day"] == "100000000"
        assert form["human_approval_above"] == "25000000"

    def test_seconds_stay_ints(self) -> None:
        form = normalized_form(load_mandate(VALID))
        assert form["retry_cooldown_seconds"] == 3600
        assert isinstance(form["retry_cooldown_seconds"], int)

    def test_addresses_are_lowercase_and_sorted(self) -> None:
        form = normalized_form(load_mandate(VALID))
        allowed_recipients = form["allowed_recipients"]
        assert allowed_recipients == sorted(allowed_recipients)
        assert all(address == address.lower() for address in allowed_recipients)

    def test_keys_are_sorted_in_the_bytes(self) -> None:
        import re

        text = normalized_bytes(load_mandate(VALID)).decode("utf-8")
        # Hanya kunci yang diikuti titik dua; nilai allowed_recipients ("0x...") tidak.
        keys = re.findall(r'"([a-z_]+)":', text)
        assert keys == sorted(keys), keys
        assert set(keys) == set(normalized_form(load_mandate(VALID)))
        assert json.loads(text)

    def test_bytes_have_no_whitespace(self) -> None:
        assert b" " not in normalized_bytes(load_mandate(VALID))


class TestHashStability:
    """ST-07: format berbeda -> hash sama; makna berbeda -> hash berbeda."""

    def test_hash_is_64_lowercase_hex(self) -> None:
        value = compute_mandate_hash(load_mandate(VALID))
        assert len(value) == 64
        assert value == value.lower()
        assert all(character in "0123456789abcdef" for character in value)

    def test_deterministic(self) -> None:
        assert len({hash_of() for _ in range(10)}) == 1

    def test_key_order_does_not_change_the_hash(self) -> None:
        shuffled = dict(reversed(list(VALID.items())))
        assert compute_mandate_hash(load_mandate(shuffled)) == hash_of()

    def test_allowlist_order_does_not_change_the_hash(self) -> None:
        assert hash_of(allowed_recipients=list(reversed(VALID["allowed_recipients"]))) == hash_of()

    def test_address_case_does_not_change_the_hash(self) -> None:
        lowered = [address.lower() for address in VALID["allowed_recipients"]]
        uppered = ["0x" + address[2:].upper() for address in VALID["allowed_recipients"]]
        assert hash_of(allowed_recipients=lowered) == hash_of()
        assert hash_of(allowed_recipients=uppered) == hash_of()

    def test_amount_spelling_does_not_change_the_hash(self) -> None:
        """ "25", "25.0", dan "25.000000" adalah jumlah yang sama."""
        assert hash_of(max_per_tx="25.0") == hash_of()
        assert hash_of(max_per_tx="25.000000") == hash_of()

    def test_duplicate_in_allowlist_does_not_change_the_hash(self) -> None:
        doubled = [*VALID["allowed_recipients"], VALID["allowed_recipients"][0].lower()]
        assert hash_of(allowed_recipients=doubled) == hash_of()

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            # Naik, bukan turun: menurunkannya di bawah human_approval_above
            # akan melanggar aturan urutan batas, bukan menguji hash.
            ("max_per_tx", "26"),
            ("max_per_day", "101"),
            ("human_approval_above", "24"),
            ("retry_cooldown_seconds", 3601),
            ("human_wait_ttl_seconds", 901),
            ("decision_ttl_seconds", 901),
            ("mandate_id", "demo-2026-12"),
            ("agent_id", "demo-agent-2"),
        ],
    )
    def test_one_changed_field_changes_the_hash(self, field: str, value: object) -> None:
        assert hash_of(**{field: value}) != hash_of()

    def test_one_changed_address_changes_the_hash(self) -> None:
        changed = [VALID["allowed_recipients"][0], "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB"]
        assert hash_of(allowed_recipients=changed) != hash_of()

    def test_removing_an_address_changes_the_hash(self) -> None:
        assert hash_of(allowed_recipients=[VALID["allowed_recipients"][0]]) != hash_of()

    def test_smallest_possible_amount_change_is_visible(self) -> None:
        """Satu base unit, yaitu 0.000001 USDC.

        Dipakai max_per_day: menurunkan max_per_tx satu unit akan melanggar
        aturan urutan batas lebih dulu, jadi ia tidak akan sampai ke hash.
        """
        assert hash_of(max_per_day="99.999999") != hash_of()
        assert hash_of(max_per_day="100.000001") != hash_of()


class TestHashConstruction:
    def test_prefix_is_the_documented_one(self) -> None:
        assert MANDATE_HASH_PREFIX == b"payment-guard/mandate/v1\n"

    def test_hash_is_sha256_of_prefix_plus_canonical_bytes(self) -> None:
        """Dihitung ulang dari spesifikasi, bukan dari implementasinya."""
        mandate = load_mandate(VALID)
        expected = hashlib.sha256(
            b"payment-guard/mandate/v1\n" + canonical_bytes(normalized_form(mandate))
        ).hexdigest()
        assert compute_mandate_hash(mandate) == expected

    def test_prefix_actually_participates(self) -> None:
        """Tanpa awalan, hash-nya berbeda. Penjaga kalau awalannya terlupa."""
        mandate = load_mandate(VALID)
        without_prefix = hashlib.sha256(normalized_bytes(mandate)).hexdigest()
        assert compute_mandate_hash(mandate) != without_prefix

    def test_hash_matches_the_database_check(self) -> None:
        """0001_init.sql: mandate_hash ~ '^[0-9a-f]{64}$'."""
        import re

        assert re.fullmatch(r"[0-9a-f]{64}", compute_mandate_hash(load_mandate(VALID)))


class TestPurity:
    def test_mandate_can_be_rebuilt_from_its_own_fields(self) -> None:
        mandate = load_mandate(VALID)
        rebuilt = Mandate(
            mandate_id=mandate.mandate_id,
            agent_id=mandate.agent_id,
            chain=mandate.chain,
            allowed_recipients=mandate.allowed_recipients,
            max_per_tx=mandate.max_per_tx,
            max_per_day=mandate.max_per_day,
            human_approval_above=mandate.human_approval_above,
            retry_cooldown_seconds=mandate.retry_cooldown_seconds,
            aggregation_window_seconds=mandate.aggregation_window_seconds,
            decision_ttl_seconds=mandate.decision_ttl_seconds,
            human_wait_ttl_seconds=mandate.human_wait_ttl_seconds,
        )
        assert compute_mandate_hash(rebuilt) == compute_mandate_hash(mandate)

    def test_json_round_trip_keeps_the_hash(self) -> None:
        """File yang ditulis ulang oleh json.dumps tetap memberi hash sama."""
        reparsed = json.loads(json.dumps(VALID))
        assert compute_mandate_hash(load_mandate(reparsed)) == hash_of()


# --------------------------------------------------------------------------- #
# Skema PRD v2 bagian 6.1
# --------------------------------------------------------------------------- #


class TestChainField:
    """PRD v2 bagian 6.2: `chain` hanya `base-sepolia`."""

    def test_accepted(self) -> None:
        assert load_mandate(VALID).chain == "base-sepolia"

    def test_is_required(self) -> None:
        data = copy.deepcopy(VALID)
        del data["chain"]
        with pytest.raises(MandateError, match=r"field wajib tidak ada.*chain"):
            load_mandate(data)

    @pytest.mark.parametrize(
        "chain",
        [
            "base-mainnet",
            "ethereum",
            "base",
            "sepolia",
            "Base-Sepolia",
            "BASE-SEPOLIA",
            "base-sepolia ",
            " base-sepolia",
            "base-sepolia\n",
            "",
            "84532",
        ],
    )
    def test_other_values_rejected(self, chain: str) -> None:
        with pytest.raises(MandateError, match="chain"):
            load_mandate(mandate_with(chain=chain))

    @pytest.mark.parametrize("value", [None, 84532, True, ["base-sepolia"]])
    def test_non_string_rejected(self, value: object) -> None:
        with pytest.raises(MandateError, match="chain harus berupa string"):
            load_mandate(mandate_with(chain=value))

    def test_message_lists_the_supported_chains(self) -> None:
        with pytest.raises(MandateError) as exc:
            load_mandate(mandate_with(chain="ethereum"))
        assert "base-sepolia" in str(exc.value)

    def test_mandate_stores_the_string_not_the_chain_id(self) -> None:
        """Pemetaan ke 84532 ada di chains.py, bukan di mandate."""
        mandate = load_mandate(VALID)
        assert mandate.chain == "base-sepolia"
        assert not hasattr(mandate, "chain_id")
        assert "chain_id" not in normalized_form(mandate)

    def test_chain_id_is_derivable_from_the_stored_string(self) -> None:
        from payment_guard.chains import chain_id_for

        assert chain_id_for(load_mandate(VALID).chain) == 84532


class TestAggregationWindow:
    """PRD v2 bagian 6.3: jendela untuk committed_recipient."""

    def test_accepted(self) -> None:
        assert load_mandate(VALID).aggregation_window_seconds == 3600

    def test_is_required(self) -> None:
        data = copy.deepcopy(VALID)
        del data["aggregation_window_seconds"]
        with pytest.raises(MandateError, match=r"field wajib tidak ada.*aggregation_window"):
            load_mandate(data)

    @pytest.mark.parametrize("value", [0, -1, -3600])
    def test_must_be_positive(self, value: int) -> None:
        with pytest.raises(MandateError, match="aggregation_window_seconds"):
            load_mandate(mandate_with(aggregation_window_seconds=value))

    @pytest.mark.parametrize("value", ["3600", 3600.0, None, True])
    def test_must_be_int(self, value: object) -> None:
        with pytest.raises(MandateError, match="aggregation_window_seconds"):
            load_mandate(mandate_with(aggregation_window_seconds=value))

    def test_one_second_is_accepted(self) -> None:
        assert load_mandate(mandate_with(aggregation_window_seconds=1)) is not None

    def test_changing_it_changes_the_hash(self) -> None:
        assert hash_of(aggregation_window_seconds=7200) != hash_of()


class TestRenamedFieldIsRejected:
    """Nama lama ``allowlist`` sekarang adalah field tak dikenal."""

    def test_old_name_alone_is_reported_as_a_missing_required_field(self) -> None:
        data = copy.deepcopy(VALID)
        data["allowlist"] = data.pop("allowed_recipients")
        with pytest.raises(MandateError) as exc:
            load_mandate(data)
        assert "allowed_recipients" in str(exc.value)

    def test_both_names_together_is_rejected_as_unknown(self) -> None:
        data = copy.deepcopy(VALID)
        data["allowlist"] = data["allowed_recipients"]
        with pytest.raises(MandateError, match="field tidak dikenal: allowlist"):
            load_mandate(data)

    def test_mandate_has_no_allowlist_attribute(self) -> None:
        assert not hasattr(load_mandate(VALID), "allowlist")
        assert "allowlist" not in normalized_form(load_mandate(VALID))


class TestFieldSetMatchesPrdV2:
    #: Sebelas field PRD v2 bagian 6.1, disalin dari dokumen.
    PRD_V2_FIELDS = frozenset(
        {
            "mandate_id",
            "agent_id",
            "chain",
            "max_per_tx",
            "max_per_day",
            "human_approval_above",
            "allowed_recipients",
            "retry_cooldown_seconds",
            "aggregation_window_seconds",
            "decision_ttl_seconds",
            "human_wait_ttl_seconds",
        }
    )

    def test_required_fields_are_exactly_the_prd_v2_set(self) -> None:
        from payment_guard.mandate import REQUIRED_FIELDS

        assert REQUIRED_FIELDS == self.PRD_V2_FIELDS, (
            f"hanya di kode: {sorted(REQUIRED_FIELDS - self.PRD_V2_FIELDS)}; "
            f"hanya di PRD v2: {sorted(self.PRD_V2_FIELDS - REQUIRED_FIELDS)}"
        )

    def test_there_are_eleven(self) -> None:
        from payment_guard.mandate import REQUIRED_FIELDS

        assert len(REQUIRED_FIELDS) == 11

    def test_normalised_form_has_the_same_keys(self) -> None:
        assert set(normalized_form(load_mandate(VALID))) == self.PRD_V2_FIELDS

    def test_dataclass_has_the_same_fields(self) -> None:
        assert set(Mandate.__dataclass_fields__) == self.PRD_V2_FIELDS


class TestGoldenHash:
    """Mandate contoh PRD v2 bagian 6.1, dengan hash yang dipaku.

    Hash di bawah dihitung sekali lalu ditulis sebagai literal. Ia **tidak**
    dihitung ulang di sini dengan kode yang sama, karena tes yang menghitung
    ulang hanya membuktikan kode konsisten dengan dirinya sendiri dan akan
    ikut berubah diam-diam bila normalisasinya berubah.

    Kalau tes ini gagal, identitas mandate berubah: setiap baris ``mandates``
    dan ``active_mandates`` yang sudah ada tidak lagi cocok. Itu keputusan
    operator, bukan sesuatu yang diperbaiki dengan menimpa literal di bawah.
    """

    #: Contoh PRD v2 bagian 6.1 apa adanya. Dua alamatnya diambil dari vektor
    #: EIP-55 yang dipakai di tes lain.
    PRD_V2_EXAMPLE: ClassVar[dict[str, Any]] = {
        "mandate_id": "m-001",
        "agent_id": "demo-agent",
        "chain": "base-sepolia",
        "max_per_tx": "50",
        "max_per_day": "200",
        "human_approval_above": "25",
        "allowed_recipients": [
            "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
            "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
        ],
        "retry_cooldown_seconds": 600,
        "aggregation_window_seconds": 3600,
        "decision_ttl_seconds": 900,
        "human_wait_ttl_seconds": 3600,
    }

    #: Dipaku. Jangan diperbarui tanpa keputusan operator.
    GOLDEN_HASH = "1e940eab68a47c319a250eeafadbdb7c0917c414744996a4a3254f79e47377ec"

    #: Byte kanonik yang menghasilkan hash itu. Dipaku juga, supaya kegagalan
    #: menunjukkan APA yang berubah, bukan sekadar bahwa sesuatu berubah.
    GOLDEN_CANONICAL = (
        '{"agent_id":"demo-agent","aggregation_window_seconds":3600,'
        '"allowed_recipients":["0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed",'
        '"0xfb6916095ca1df60bb79ce92ce3ea74c37c5d359"],"chain":"base-sepolia",'
        '"decision_ttl_seconds":900,"human_approval_above":"25000000",'
        '"human_wait_ttl_seconds":3600,"mandate_id":"m-001",'
        '"max_per_day":"200000000","max_per_tx":"50000000",'
        '"retry_cooldown_seconds":600}'
    )

    def test_prd_v2_example_loads(self) -> None:
        """Yang gagal sebelum perbaikan skema: contoh dokumen ditolak."""
        mandate = load_mandate(self.PRD_V2_EXAMPLE)
        assert mandate.mandate_id == "m-001"
        assert mandate.chain == "base-sepolia"
        assert mandate.max_per_tx == 50_000_000
        assert mandate.max_per_day == 200_000_000
        assert mandate.human_approval_above == 25_000_000
        assert mandate.retry_cooldown_seconds == 600
        assert mandate.aggregation_window_seconds == 3600
        assert mandate.decision_ttl_seconds == 900
        assert mandate.human_wait_ttl_seconds == 3600

    def test_hash_matches_the_pinned_value(self) -> None:
        assert compute_mandate_hash(load_mandate(self.PRD_V2_EXAMPLE)) == self.GOLDEN_HASH

    def test_canonical_bytes_match_the_pinned_value(self) -> None:
        mandate = load_mandate(self.PRD_V2_EXAMPLE)
        assert normalized_bytes(mandate).decode("utf-8") == self.GOLDEN_CANONICAL

    def test_pinned_hash_has_the_database_shape(self) -> None:
        import re

        assert re.fullmatch(r"[0-9a-f]{64}", self.GOLDEN_HASH)

    def test_key_order_in_the_example_does_not_matter(self) -> None:
        shuffled = dict(reversed(list(self.PRD_V2_EXAMPLE.items())))
        assert compute_mandate_hash(load_mandate(shuffled)) == self.GOLDEN_HASH

    def test_lowercase_addresses_in_the_example_give_the_same_hash(self) -> None:
        data = copy.deepcopy(self.PRD_V2_EXAMPLE)
        data["allowed_recipients"] = [a.lower() for a in data["allowed_recipients"]]
        assert compute_mandate_hash(load_mandate(data)) == self.GOLDEN_HASH

    def test_domain_prefix_is_unchanged(self) -> None:
        assert MANDATE_HASH_PREFIX == b"payment-guard/mandate/v1\n"
