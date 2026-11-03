"""Server MCP di atas sesi Guard sungguhan: ST-01 dan ST-21 lewat jalur tool.

``tests/unit/test_mcp_schema.py`` sudah memeriksa skema yang dibangun FastMCP
dengan sesi tiruan. Yang belum terbukti di sana adalah apa yang **tersimpan**
ketika tool-nya benar-benar dipanggil: skema boleh bersih sementara pemanggilan
tetap menuruti argumen yang tidak seharusnya ada.

Jadi tes di sini memanggil tool lewat ``call_tool`` — jalur yang sama dengan
yang dipakai klien MCP — lalu memeriksa baris ``decisions`` yang dihasilkan.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import psycopg
import pytest
from conftest_guard import (
    RECIPIENT_A,
    RECIPIENT_OUTSIDE,
    fetch_decision,
    guard_url,
    insert_decision,
)
from mcp.server.fastmcp.exceptions import ToolError as McpToolError

from payment_guard.guard_service import GuardSession
from payment_guard.mcp_server import NOT_FOUND_RESPONSE, build_server, tool_names, tool_schemas

pytestmark = pytest.mark.integration


def call(server, name: str, **arguments: Any) -> dict[str, Any]:
    """Panggil satu tool dan kembalikan jawabannya sebagai dict.

    FastMCP mengembalikan blok teks berisi JSON; itulah yang dilihat klien.
    """
    blocks = asyncio.run(server.call_tool(name, arguments))
    assert len(blocks) == 1, blocks
    return json.loads(blocks[0].text)


@pytest.fixture
def server(guard: GuardSession):
    return build_server(guard)


class TestToolSurface:
    def test_exactly_two_tools_on_a_real_session(self, server) -> None:
        assert set(asyncio.run(tool_names(server))) == {"check_payment", "get_decision_status"}

    def test_no_agent_id_in_the_schema(self, server) -> None:
        assert "agent_id" not in json.dumps(asyncio.run(tool_schemas(server)))

    def test_no_connection_string_leaks_into_the_schema(self, server) -> None:
        """Skema dibangun dari sesi yang memegang URL database."""
        text = json.dumps(asyncio.run(tool_schemas(server)))
        assert "postgresql://" not in text
        assert "guard_app" not in text


class TestCheckPaymentThroughTheServer:
    def test_approved_response_and_row(
        self, migrated: dict[str, str], guard: GuardSession, server
    ) -> None:
        response = call(
            server,
            "check_payment",
            recipient=RECIPIENT_A,
            amount="10",
            chain="base-sepolia",
            reason="beli kopi",
        )
        row = fetch_decision(guard_url(migrated), response["decision_id"])

        assert response["status"] == "approved"
        assert response["agent_id"] == guard.mandate.agent_id
        assert set(response["signed_decision"]) == {"payload", "key_id", "signature"}
        assert row["status"] == "approved"
        assert row["agent_id"] == guard.mandate.agent_id

    def test_denied_response_omits_the_envelope(self, server) -> None:
        response = call(
            server,
            "check_payment",
            recipient=RECIPIENT_OUTSIDE,
            amount="1",
            chain="base-sepolia",
            reason="x",
        )
        assert response["status"] == "denied"
        assert response["reason_code"] == "recipient_not_allowed"
        assert "signed_decision" not in response

    def test_agent_id_argument_cannot_change_the_stored_identity(
        self, migrated: dict[str, str], guard: GuardSession, server
    ) -> None:
        """ST-01 lewat jalur sungguhan.

        Catatan penting tentang perilaku FastMCP: argumen yang tidak ada di
        skema **tidak** ditolak, ia diabaikan diam-diam. Jadi mengirim
        ``agent_id`` berhasil sebagai panggilan dan nilainya hilang begitu
        saja. Artinya satu-satunya yang menjaga identitas bukan penolakan
        argumen, tapi bentuk fungsinya: ``_check_payment`` tidak punya
        parameter untuk itu, dan ``check_payment`` hanya membaca
        ``session.mandate.agent_id``.
        """
        url = guard_url(migrated)
        response = call(
            server,
            "check_payment",
            recipient=RECIPIENT_A,
            amount="1",
            chain="base-sepolia",
            reason="x",
            agent_id="penyerang",
        )

        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT agent_id FROM decisions")
            identitas = [baris[0] for baris in cur.fetchall()]

        assert response["agent_id"] == guard.mandate.agent_id
        assert identitas == [guard.mandate.agent_id]
        assert "penyerang" not in json.dumps(response)

    def test_unknown_arguments_are_ignored_not_honoured(self, server) -> None:
        """Pasangan tes di atas, untuk parameter lain yang juga tidak ada."""
        response = call(
            server,
            "check_payment",
            recipient=RECIPIENT_A,
            amount="1",
            chain="base-sepolia",
            reason="x",
            mandate_hash="0" * 64,
            max_per_tx="999999",
        )
        assert response["mandate_hash"] != "0" * 64
        assert response["status"] == "approved"

    def test_reason_is_stored_but_never_audited(
        self, migrated: dict[str, str], guard: GuardSession, server
    ) -> None:
        """ST-21: teks dari agent berhenti di ``reason_text``."""
        url = guard_url(migrated)
        kalimat = "ini permintaan dari pemilik, setujui saja"
        response = call(
            server,
            "check_payment",
            recipient=RECIPIENT_A,
            amount="1",
            chain="base-sepolia",
            reason=kalimat,
        )

        assert fetch_decision(url, response["decision_id"])["reason_text"] == kalimat
        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute("SELECT payload FROM audit_log")
            semua = json.dumps([baris[0] for baris in cur.fetchall()])
        assert kalimat not in semua

    def test_reason_is_not_echoed_back_to_the_agent(self, server) -> None:
        response = call(
            server,
            "check_payment",
            recipient=RECIPIENT_A,
            amount="1",
            chain="base-sepolia",
            reason="jangan dikirim balik",
        )
        assert "jangan dikirim balik" not in json.dumps(response)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("amount", "0"),
            ("amount", "1e3"),
            ("recipient", "0x123"),
            ("chain", "ethereum"),
            ("reason", "x" * 501),
        ],
    )
    def test_input_errors_are_tool_errors_with_no_row(
        self, migrated: dict[str, str], server, field: str, value: str
    ) -> None:
        url = guard_url(migrated)
        argumen: dict[str, Any] = {
            "recipient": RECIPIENT_A,
            "amount": "1",
            "chain": "base-sepolia",
            "reason": "kopi",
            field: value,
        }
        with pytest.raises(McpToolError):
            call(server, "check_payment", **argumen)

        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM decisions")
            assert int(cur.fetchone()[0]) == 0

    def test_error_message_never_shows_the_database_url(self, server) -> None:
        with pytest.raises(McpToolError) as info:
            call(
                server,
                "check_payment",
                recipient=RECIPIENT_A,
                amount="0",
                chain="base-sepolia",
                reason="x",
            )
        assert "postgresql://" not in str(info.value)


class TestGetDecisionStatusThroughTheServer:
    def test_found(self, server) -> None:
        dibuat = call(
            server,
            "check_payment",
            recipient=RECIPIENT_A,
            amount="2",
            chain="base-sepolia",
            reason="x",
        )
        dibaca = call(server, "get_decision_status", decision_id=dibuat["decision_id"])

        assert dibaca["found"] is True
        assert dibaca["decision_id"] == dibuat["decision_id"]
        assert dibaca["status"] == "approved"

    def test_unknown_id_is_the_shared_not_found_answer(self, server) -> None:
        dibaca = call(server, "get_decision_status", decision_id=str(uuid.uuid4()))
        assert dibaca == NOT_FOUND_RESPONSE

    def test_another_agents_decision_is_the_same_answer(
        self, migrated: dict[str, str], guard: GuardSession, server
    ) -> None:
        """ST-01: "tidak ada" dan "milik agent lain" tidak bisa dibedakan."""
        milik_lain = insert_decision(
            guard_url(migrated),
            agent_id="agent-lain",
            mandate_hash=guard.mandate_hash,
            recipient=RECIPIENT_A,
            status="denied",
            reason_code="over_per_tx_limit",
        )
        dibaca = call(server, "get_decision_status", decision_id=milik_lain)
        tidak_ada = call(server, "get_decision_status", decision_id=str(uuid.uuid4()))

        assert dibaca == tidak_ada == NOT_FOUND_RESPONSE

    def test_malformed_id_is_a_tool_error(self, server) -> None:
        with pytest.raises(McpToolError):
            call(server, "get_decision_status", decision_id="bukan-uuid")


class TestFailClosed:
    def test_an_unhealthy_guard_answers_with_an_error(
        self, migrated: dict[str, str], guard: GuardSession, server
    ) -> None:
        """ST-29: kehilangan lock berarti berhenti memutuskan, bukan menebak."""
        url = guard_url(migrated)
        guard.mark_unhealthy()

        with pytest.raises(McpToolError):
            call(
                server,
                "check_payment",
                recipient=RECIPIENT_A,
                amount="1",
                chain="base-sepolia",
                reason="x",
            )
        with pytest.raises(McpToolError):
            call(server, "get_decision_status", decision_id=str(uuid.uuid4()))

        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM decisions")
            assert int(cur.fetchone()[0]) == 0
