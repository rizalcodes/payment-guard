"""Harness eval: registri tool, Executor stub, dan penghentian total.

Satu run penuh butuh Guard sungguhan dan Postgres, jadi itu tes integrasi.
Yang diuji di sini adalah bagian-bagian yang bisa diperiksa tanpa keduanya —
dan yang paling penting di antaranya: **Executor eval tidak punya jalan ke
chain maupun ke database.**
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from evals.adapters import ToolCall
from evals.harness import (
    MAX_TURNS,
    TOOL_SPECS,
    BroadcastAttempted,
    HarnessError,
    _run_executor,
    _text_of,
    agent_tool_names,
    guard_tool_names,
    tool_spec_names,
)
from payment_guard import demo_agent


class FakeBlock:
    def __init__(self, text: str | None) -> None:
        self.type = "text"
        if text is not None:
            self.text = text


class FakeToolResult:
    def __init__(self, blocks: list[Any]) -> None:
        self.content = blocks


@pytest.fixture
def public_keys(tmp_path: Path) -> Path:
    """Berkas kunci publik sungguhan, dari ``run_keygen``.

    Bukan JSON yang ditulis tangan: berkas kunci yang bentuknya salah akan
    membuat Executor melempar sebelum sampai ke verifikasi, dan tes yang
    lolos karena itu tidak membuktikan apa pun tentang verifikasinya.
    """
    from payment_guard.keygen import run_keygen

    rahasia = tmp_path / "rahasia" / "signing.json"
    publik = tmp_path / "public.json"
    run_keygen(rahasia, publik)
    return publik


class TestToolRegistryST22:
    """ST-22: agent melihat **tepat tiga** tool, dan tidak satu pun lebih."""

    def test_exactly_three_tools(self) -> None:
        assert len(TOOL_SPECS) == 3
        assert len(tool_spec_names()) == 3

    def test_matches_the_agent_registry(self) -> None:
        """Daftar harness harus identik dengan daftar agent sungguhan.

        Kalau keduanya bisa berbeda, eval akan mengukur agent yang tidak ada.
        """
        assert tool_spec_names() == agent_tool_names()

    def test_two_come_from_guard_one_is_local(self) -> None:
        guard = set(guard_tool_names())
        semua = set(tool_spec_names())
        assert guard == {"check_payment", "get_decision_status"}
        assert semua - guard == {"execute_payment"}

    def test_no_tool_grants_direct_chain_access(self) -> None:
        """Tidak ada tool bernama transfer, send, sign, atau sql."""
        terlarang = ("transfer", "send_", "sign", "sql", "query", "broadcast", "wallet")
        for nama in tool_spec_names():
            assert not any(kata in nama for kata in terlarang), nama

    def test_schemas_are_closed(self) -> None:
        """``additionalProperties: false`` di setiap skema.

        Skema terbuka membiarkan model mengirim field apa pun tanpa terlihat
        sebagai kesalahan, dan argumen yang tidak terlihat tidak bisa dinilai.
        """
        for spec in TOOL_SPECS:
            assert spec.parameters["additionalProperties"] is False

    def test_required_matches_scoring_expectations(self) -> None:
        from evals.scoring import REQUIRED_ARGUMENTS

        for spec in TOOL_SPECS:
            assert tuple(spec.parameters["required"]) == REQUIRED_ARGUMENTS[spec.name]

    def test_descriptions_do_not_leak_the_answer(self) -> None:
        """Deskripsi tool tidak boleh memberi tahu model cara lulus.

        Prompt yang menyebut "jangan bayar kalau ditolak" akan mengukur
        kepatuhan pada prompt, bukan perilaku yang ingin diukur.
        """
        for spec in TOOL_SPECS:
            rendah = spec.description.lower()
            assert "jangan" not in rendah
            assert "tolak" not in rendah
            assert "berhenti" not in rendah


class TestTurnLimit:
    def test_allows_the_legitimate_path_plus_room(self) -> None:
        """Tiga putaran cukup untuk jalur sah; batasnya memberi ruang variasi."""
        assert MAX_TURNS > 3
        assert MAX_TURNS <= 10


class TestTextOf:
    def test_reads_single_json_block(self) -> None:
        hasil = FakeToolResult([FakeBlock(json.dumps({"status": "denied"}))])
        assert _text_of(hasil) == {"status": "denied"}

    def test_rejects_multiple_blocks(self) -> None:
        hasil = FakeToolResult([FakeBlock("{}"), FakeBlock("{}")])
        with pytest.raises(HarnessError, match="2 blok"):
            _text_of(hasil)

    def test_rejects_non_text_block(self) -> None:
        hasil = FakeToolResult([FakeBlock(None)])
        with pytest.raises(HarnessError, match="bukan teks"):
            _text_of(hasil)

    def test_rejects_non_object_json(self) -> None:
        hasil = FakeToolResult([FakeBlock("[1, 2, 3]")])
        with pytest.raises(HarnessError, match="bukan objek JSON"):
            _text_of(hasil)


class TestExecutorIsAStub:
    """Executor eval adalah stub yang sudah ada, dan ia tidak bisa membayar."""

    def test_no_new_mock_was_written(self) -> None:
        """Harness memakai ``demo_agent.execute_payment`` apa adanya.

        Mock baru berarti satu jalur lagi yang bisa berbeda dari jalur
        sungguhan, dan eval yang mengukur mock-nya sendiri tidak mengukur
        apa pun.
        """
        import evals.harness as harness

        assert harness.execute_payment is demo_agent.execute_payment

    def test_demo_agent_imports_neither_db_nor_chain(self) -> None:
        """Dibuktikan dari impornya, bukan dari perilaku satu pemanggilan.

        Perilaku satu pemanggilan hanya membuktikan jalur itu; daftar impor
        membuktikan tidak ada jalur sama sekali.
        """
        import ast

        sumber = Path(demo_agent.__file__).read_text(encoding="utf-8")
        modul = ast.parse(sumber)
        diimpor: set[str] = set()
        for node in ast.walk(modul):
            if isinstance(node, ast.Import):
                diimpor.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                diimpor.add(node.module)

        for terlarang in ("psycopg", "web3", "payment_guard.chain", "payment_guard.executor"):
            assert terlarang not in diimpor, f"{terlarang} diimpor demo_agent"

    def test_invalid_envelope_is_not_verified_and_not_broadcast(self, public_keys: Path) -> None:
        kunci = public_keys
        panggilan = ToolCall(
            name="execute_payment",
            arguments={"signed_decision": {"palsu": True}, "expected_mandate_hash": "ab"},
        )
        jawaban, catatan = _run_executor(panggilan, public_keys_path=kunci)

        assert jawaban["verified"] is False
        assert jawaban["broadcast"] is False
        assert catatan.broadcast is False

    def test_missing_arguments_do_not_crash_the_run(self, public_keys: Path) -> None:
        """Argumen yang hilang adalah hasil eval yang sah, bukan error harness."""
        kunci = public_keys
        panggilan = ToolCall(name="execute_payment", arguments={})
        jawaban, _ = _run_executor(panggilan, public_keys_path=kunci)
        assert jawaban["verified"] is False

    def test_no_database_connection_is_opened(
        self, public_keys: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``psycopg.connect`` diganti dengan yang meledak. Ia tidak boleh dipanggil."""
        import psycopg

        def meledak(*args: object, **kwargs: object) -> None:
            raise AssertionError("Executor eval membuka koneksi database")

        monkeypatch.setattr(psycopg, "connect", meledak)

        panggilan = ToolCall(
            name="execute_payment",
            arguments={"signed_decision": {"x": 1}, "expected_mandate_hash": "ab"},
        )
        _run_executor(panggilan, public_keys_path=public_keys)

    def test_no_rpc_client_is_built(
        self, public_keys: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``Web3Client`` diganti dengan yang meledak. Ia tidak boleh dibangun."""
        from payment_guard import chain

        def meledak(*args: object, **kwargs: object) -> None:
            raise AssertionError("Executor eval membangun klien RPC")

        monkeypatch.setattr(chain, "Web3Client", meledak)

        panggilan = ToolCall(
            name="execute_payment",
            arguments={"signed_decision": {"x": 1}, "expected_mandate_hash": "ab"},
        )
        _run_executor(panggilan, public_keys_path=public_keys)


class TestBroadcastStopsEverything:
    """``broadcast=True`` menghentikan eval total, bukan menggagalkan satu run."""

    def test_raises_on_broadcast(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Stub tidak punya jalur yang bisa menghasilkan ini.

        Kalau ia terjadi, yang salah bukan skor model melainkan asumsi dasar
        eval, dan melanjutkan berarti membelanjakan uang untuk mengukur
        sesuatu yang sudah tidak dipahami.
        """
        import evals.harness as harness

        class Siaran:
            verified = True
            detail = "terkirim"
            decision_id = "abc"
            broadcast = True

        monkeypatch.setattr(harness, "execute_payment", lambda *a, **k: Siaran())

        panggilan = ToolCall(
            name="execute_payment",
            arguments={"signed_decision": {}, "expected_mandate_hash": "ab"},
        )
        with pytest.raises(BroadcastAttempted, match="berhenti total"):
            harness._run_executor(panggilan, public_keys_path=tmp_path / "public.json")

    def test_is_not_a_subclass_of_harness_error(self) -> None:
        """Ia tidak boleh tertangkap penangan error biasa di dalam loop run."""
        assert not issubclass(BroadcastAttempted, HarnessError)


class TestReadOrdering:
    """Pembacaan ``active_mandates`` terjadi **setelah** Guard start.

    Urutan ini pernah salah, dan kesalahannya tidak terlihat di jalur bawaan.
    Yang mengisi ``active_mandates`` adalah Guard sendiri saat start
    (``guard_service.UPSERT_ACTIVE_MANDATE``), jadi membacanya sebelum
    ``initialize`` berarti membaca tabel kosong — lalu membandingkan ``None``
    dengan hash yang sah dan menyebutnya "mandate berganti".

    Diuji dengan transport tiruan: yang diperiksa adalah urutan pemanggilan,
    dan itu tidak butuh Guard maupun Postgres.
    """

    @staticmethod
    def _jalankan(jejak: list[str], monkeypatch: pytest.MonkeyPatch) -> object:
        """Satu run di atas MCP tiruan, mencatat urutan tiap langkah."""
        import asyncio
        from contextlib import asynccontextmanager

        import evals.harness as harness
        from evals.adapters import ModelReply, Usage
        from evals.scenarios import E4

        class FakeTool:
            def __init__(self, nama: str) -> None:
                self.name = nama

        class FakeToolList:
            tools: ClassVar[list[FakeTool]] = [FakeTool(n) for n in harness.GUARD_TOOL_NAMES]

        class FakeSession:
            def __init__(self, *_: object) -> None:
                pass

            async def __aenter__(self) -> FakeSession:
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            async def initialize(self) -> None:
                jejak.append("initialize")

            async def list_tools(self) -> FakeToolList:
                jejak.append("list_tools")
                return FakeToolList()

        @asynccontextmanager
        async def fake_stdio(_: object):  # type: ignore[no-untyped-def]
            jejak.append("spawn")
            yield (None, None)

        def fake_mandate(_url: str, _agent: str) -> str:
            jejak.append("baca_mandate")
            return "hash-eval"

        def fake_statuses(_url: str) -> dict[str, str]:
            jejak.append("baca_decisions")
            return {}

        class Diam:
            model_name = "diam"
            model_version = "diam"
            temperature = "0"

            def reply(self, **_: object) -> ModelReply:
                jejak.append("model")
                return ModelReply(
                    text="tidak melakukan apa pun",
                    tool_calls=(),
                    usage=Usage(0, 0),
                    stop_reason="end_turn",
                    raw={},
                )

            def cost_micro_usd(self, usage: Usage) -> int:
                return 0

        monkeypatch.setattr(harness, "stdio_client", fake_stdio)
        monkeypatch.setattr(harness, "ClientSession", FakeSession)
        monkeypatch.setattr(harness, "active_mandate_hash", fake_mandate)
        monkeypatch.setattr(harness, "decision_statuses", fake_statuses)

        return asyncio.run(
            harness.run_once(
                adapter=Diam(),  # type: ignore[arg-type]
                scenario=E4,
                env_file=Path("tidak-dibuka.env"),
                public_keys_path=Path("tidak-dibuka.json"),
                admin_url="postgresql://tidak-dipakai",
                agent_id="eval-agent",
                expected_mandate_hash="hash-eval",
            )
        )

    def test_mandate_is_read_after_initialize(self, monkeypatch: pytest.MonkeyPatch) -> None:
        jejak: list[str] = []
        self._jalankan(jejak, monkeypatch)

        assert "initialize" in jejak
        assert "baca_mandate" in jejak
        assert jejak.index("initialize") < jejak.index("baca_mandate"), jejak

    def test_mandate_is_not_read_before_the_server_is_spawned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tidak ada pembacaan sebelum subproses Guard bahkan dijalankan."""
        jejak: list[str] = []
        self._jalankan(jejak, monkeypatch)
        assert jejak.index("spawn") < jejak.index("baca_mandate"), jejak

    def test_read_twice_around_the_conversation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dua kali: satu sebelum percakapan, satu sesudah."""
        jejak: list[str] = []
        self._jalankan(jejak, monkeypatch)

        bacaan = [i for i, langkah in enumerate(jejak) if langkah == "baca_mandate"]
        assert len(bacaan) == 2, jejak
        assert bacaan[0] < jejak.index("model") < bacaan[1], jejak

    def test_expected_hash_reaches_the_observation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Nilai yang diharapkan diteruskan ke penilai, bukan dihitung ulang di sana."""
        hasil = self._jalankan([], monkeypatch)
        assert hasil.observation.mandate_hash_expected == "hash-eval"  # type: ignore[attr-defined]
