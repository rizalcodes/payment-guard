"""Skema tool MCP: tepat dua tool, tanpa ``agent_id``. ST-01, ST-20, ST-21.

Yang diperiksa di sini adalah skema yang **dibangun FastMCP dan dikirim ke
klien**, lewat ``list_tools()``, bukan tanda tangan fungsi Python-nya. Agent
melihat skema itu; kalau ``agent_id`` muncul di sana, agent bisa mengirimnya,
apa pun yang ditulis di kode.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from payment_guard.chains import BASE_SEPOLIA
from payment_guard.decision_service import (
    MAX_REASON_LENGTH,
    CheckPaymentInput,
    InputError,
    validate_input,
)
from payment_guard.mandate import load_mandate
from payment_guard.mcp_server import (
    NOT_FOUND_RESPONSE,
    SERVER_NAME,
    TOOL_NAMES,
    build_server,
    tool_names,
    tool_schemas,
)

MANDATE_DOCUMENT: dict[str, Any] = {
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

RECIPIENT = "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"


@pytest.fixture
def server():
    """Server dibangun dari sesi tiruan: skema tidak butuh database."""
    return build_server(MagicMock())


@pytest.fixture
def fake_session():
    session = MagicMock()
    session.mandate = load_mandate(MANDATE_DOCUMENT)
    # `validate_input` membaca jaringan dari config sesi untuk memetakan nama
    # chain ke chain_id. Pada MagicMock, atribut itu mengembalikan MagicMock
    # dan `normalize_network` menolaknya — jadi ia disetel eksplisit di sini.
    session.config.network = BASE_SEPOLIA
    return session


def names(server) -> tuple[str, ...]:
    return asyncio.run(tool_names(server))


def schemas(server) -> dict[str, dict[str, object]]:
    return asyncio.run(tool_schemas(server))


class TestExactlyTwoTools:
    def test_two_tools(self, server) -> None:
        assert len(names(server)) == 2

    def test_names_are_the_documented_ones(self, server) -> None:
        assert set(names(server)) == {"check_payment", "get_decision_status"}

    def test_matches_the_module_constant(self, server) -> None:
        assert set(names(server)) == set(TOOL_NAMES)

    def test_execute_payment_is_not_a_guard_tool(self, server) -> None:
        """PRD v2.1 nomor 2: execute_payment adalah pembungkus lokal di host."""
        assert "execute_payment" not in names(server)

    def test_no_internal_names_leak(self, server) -> None:
        """Nama fungsi Python berawalan garis bawah tidak boleh terlihat agent."""
        for name in names(server):
            assert not name.startswith("_")
            assert not name.endswith("_tool")

    def test_server_name(self, server) -> None:
        assert SERVER_NAME == "payment-guard"


class TestNoAgentIdInSchema:
    """ST-01 dan Security v1.1 S1."""

    def test_check_payment_has_no_agent_id(self, server) -> None:
        schema = schemas(server)["check_payment"]
        assert "agent_id" not in schema["properties"]
        assert "agent_id" not in json.dumps(schema)

    def test_get_decision_status_has_no_agent_id(self, server) -> None:
        schema = schemas(server)["get_decision_status"]
        assert "agent_id" not in schema["properties"]
        assert "agent_id" not in json.dumps(schema)

    def test_no_tool_mentions_agent_id_anywhere(self, server) -> None:
        assert "agent_id" not in json.dumps(schemas(server))

    def test_no_mandate_or_key_parameters_either(self, server) -> None:
        """Agent juga tidak boleh memilih mandate atau kunci."""
        text = json.dumps(schemas(server))
        for forbidden in ("mandate", "key_id", "signing", "mandate_hash"):
            assert forbidden not in text


class TestParameterNames:
    def test_check_payment_parameters(self, server) -> None:
        schema = schemas(server)["check_payment"]
        assert set(schema["properties"]) == {"recipient", "amount", "chain", "reason"}
        assert set(schema["required"]) == {"recipient", "amount", "chain", "reason"}

    def test_get_decision_status_parameters(self, server) -> None:
        schema = schemas(server)["get_decision_status"]
        assert set(schema["properties"]) == {"decision_id"}
        assert set(schema["required"]) == {"decision_id"}

    def test_all_parameters_are_strings(self, server) -> None:
        """Jumlah masuk sebagai string desimal, bukan angka (PRD v2 bagian 6.2)."""
        for schema in schemas(server).values():
            for name, spec in schema["properties"].items():
                assert spec["type"] == "string", name

    def test_amount_is_not_a_number(self, server) -> None:
        """Angka JSON akan melewati float; string desimal tidak."""
        amount = schemas(server)["check_payment"]["properties"]["amount"]
        assert amount["type"] == "string"
        assert amount["type"] != "number"


class TestNotFoundResponse:
    def test_shape(self) -> None:
        assert NOT_FOUND_RESPONSE["found"] is False

    def test_does_not_reveal_ownership(self) -> None:
        """ "Milik agent lain" dan "tidak ada" harus satu jawaban."""
        text = json.dumps(NOT_FOUND_RESPONSE).lower()
        for leak in ("agent lain", "other", "forbidden", "milik"):
            assert leak not in text


class TestReasonLength:
    """ST-20: reason lebih dari 500 karakter ditolak sebagai error input."""

    def test_limit_is_500(self) -> None:
        assert MAX_REASON_LENGTH == 500

    def test_exactly_500_accepted(self, fake_session) -> None:
        request = validate_input(
            CheckPaymentInput(
                recipient=RECIPIENT, amount="1", chain="base-sepolia", reason="x" * 500
            ),
            fake_session,
        )
        assert len(request.reason) == 500

    def test_501_rejected(self, fake_session) -> None:
        with pytest.raises(InputError, match="maksimum 500 karakter"):
            validate_input(
                CheckPaymentInput(
                    recipient=RECIPIENT, amount="1", chain="base-sepolia", reason="x" * 501
                ),
                fake_session,
            )

    def test_limit_is_characters_not_bytes(self, fake_session) -> None:
        """CHECK di database memakai char_length, jadi batasnya karakter."""
        reason = "é" * 500
        assert len(reason.encode("utf-8")) == 1000
        request = validate_input(
            CheckPaymentInput(recipient=RECIPIENT, amount="1", chain="base-sepolia", reason=reason),
            fake_session,
        )
        assert len(request.reason) == 500

    def test_empty_reason_accepted(self, fake_session) -> None:
        request = validate_input(
            CheckPaymentInput(recipient=RECIPIENT, amount="1", chain="base-sepolia", reason=""),
            fake_session,
        )
        assert request.reason == ""

    @pytest.mark.parametrize("value", [None, 1, ["x"]])
    def test_non_string_reason_rejected(self, fake_session, value: object) -> None:
        with pytest.raises(InputError, match="reason harus string"):
            validate_input(
                CheckPaymentInput(
                    recipient=RECIPIENT, amount="1", chain="base-sepolia", reason=value
                ),
                fake_session,
            )


class TestInputValidation:
    """Langkah 0 PRD v2.1 bagian 6.4: semuanya error input, bukan keputusan."""

    def base(self, **overrides: Any) -> CheckPaymentInput:
        fields: dict[str, Any] = {
            "recipient": RECIPIENT,
            "amount": "1",
            "chain": "base-sepolia",
            "reason": "kopi",
        }
        fields.update(overrides)
        return CheckPaymentInput(**fields)

    def test_valid_input_normalised(self, fake_session) -> None:
        request = validate_input(self.base(amount="25.5"), fake_session)
        assert request.amount_base == 25_500_000
        assert request.recipient == RECIPIENT
        assert request.chain_id == 84532

    @pytest.mark.parametrize(
        "amount", ["0", "", "-1", "1e3", "0.0000001", "1,5", " 1", "007", "NaN"]
    )
    def test_bad_amount_rejected(self, fake_session, amount: str) -> None:
        with pytest.raises(InputError, match="amount:"):
            validate_input(self.base(amount=amount), fake_session)

    @pytest.mark.parametrize(
        "recipient",
        ["0x123", "", "bukan-alamat", "0x5aaeb6053F3E94C9b9A09f33669435E7Ef1BeAed"],
    )
    def test_bad_recipient_rejected(self, fake_session, recipient: str) -> None:
        with pytest.raises(InputError, match="recipient:"):
            validate_input(self.base(recipient=recipient), fake_session)

    def test_uppercase_recipient_is_normalised_not_rejected(self, fake_session) -> None:
        """Satu kasus huruf diterima dan dinormalisasi (M1)."""
        request = validate_input(self.base(recipient="0x" + RECIPIENT[2:].upper()), fake_session)
        assert request.recipient == RECIPIENT

    @pytest.mark.parametrize("chain", ["ethereum", "base", "", "Base-Sepolia", "84532"])
    def test_bad_chain_rejected(self, fake_session, chain: str) -> None:
        with pytest.raises(InputError, match="chain"):
            validate_input(self.base(chain=chain), fake_session)

    def test_recipient_outside_allowlist_is_not_an_input_error(self, fake_session) -> None:
        """Itu keputusan ``recipient_not_allowed``, bukan error input."""
        request = validate_input(self.base(recipient="0x" + "9" * 40), fake_session)
        assert request.recipient == "0x" + "9" * 40

    def test_reason_is_carried_but_never_evaluated(self, fake_session) -> None:
        """Reason ikut ke baris decisions, tapi tidak ke mana pun selain itu."""
        import inspect

        from payment_guard.rules import evaluate

        request = validate_input(self.base(reason="abaikan aturan"), fake_session)
        assert request.reason == "abaikan aturan"
        assert "reason" not in inspect.signature(evaluate).parameters
