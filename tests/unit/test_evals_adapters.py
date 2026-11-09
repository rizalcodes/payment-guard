"""Adapter model: transport tiruan, aritmetika uang, dan penolakan.

Tidak ada panggilan jaringan di berkas ini. Adapter Ollama memakai ``opener``
suntikan; adapter Haiku memakai klien suntikan. Paket ``anthropic`` tidak
terpasang di mesin ini dan tes ini tidak membutuhkannya — itulah sebabnya
kliennya disuntik, bukan dibuat di dalam adapter.
"""

from __future__ import annotations

import io
import json
import urllib.error
from dataclasses import dataclass
from typing import Any

import pytest

from evals.adapters import (
    HAIKU_MODEL_ID,
    OLLAMA_MODEL_ID,
    PRICE_MICRO_USD_PER_MTOK,
    REDACTED,
    AdapterError,
    HaikuAdapter,
    ModelAdapter,
    OllamaAdapter,
    ToolSpec,
    Turn,
    Usage,
    cost_micro_usd,
)

TOOLS = (ToolSpec(name="check_payment", description="minta izin", parameters={"type": "object"}),)
PESAN = (Turn(role="user", content="bayar 10 USDC"),)


# --------------------------------------------------------------------------- #
# Transport tiruan
# --------------------------------------------------------------------------- #


class FakeHTTPResponse(io.BytesIO):
    """Pengganti objek jawaban ``urlopen``, cukup untuk ``with`` dan ``read``."""

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass
class FakeOpener:
    """``urlopen`` tiruan. Menyimpan body permintaan supaya bisa diperiksa."""

    payload: dict[str, Any]
    seen_body: dict[str, Any] | None = None

    def __call__(self, request: Any, timeout: int | None = None) -> FakeHTTPResponse:
        self.seen_body = json.loads(request.data.decode("utf-8"))
        return FakeHTTPResponse(json.dumps(self.payload).encode("utf-8"))


class ExplodingOpener:
    """``urlopen`` yang selalu gagal, seperti Ollama yang tidak jalan."""

    def __call__(self, request: Any, timeout: int | None = None) -> FakeHTTPResponse:
        raise urllib.error.URLError("connection refused")


class Block:
    """Blok konten tiruan dari SDK Anthropic."""

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class FakeAnthropicClient:
    """Klien tiruan dengan ``.messages.create``. Menyimpan kwargs permintaan."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.seen: dict[str, Any] | None = None
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        self.seen = kwargs
        return self._response


class FailingAnthropicClient:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        raise self._exc


def haiku_response(
    *,
    content: list[Any] | None = None,
    input_tokens: int = 1000,
    output_tokens: int = 200,
    stop_reason: str = "end_turn",
    refusal_category: str | None = None,
) -> Any:
    jawaban = Block(
        content=content if content is not None else [Block(type="text", text="selesai")],
        usage=Block(input_tokens=input_tokens, output_tokens=output_tokens),
        stop_reason=stop_reason,
    )
    jawaban.stop_details = (
        Block(category=refusal_category) if refusal_category is not None else None
    )
    return jawaban


# --------------------------------------------------------------------------- #
# Aritmetika uang
# --------------------------------------------------------------------------- #


class TestCostArithmetic:
    """Uang dihitung dengan bilangan bulat micro-USD, dan dibulatkan ke atas."""

    def test_haiku_price_from_verified_table(self) -> None:
        harga = PRICE_MICRO_USD_PER_MTOK[HAIKU_MODEL_ID]
        assert harga == {"input": 100_000, "output": 500_000}

    def test_typical_run(self) -> None:
        """6.000 input + 2.400 output = 1.800 micro-USD ($0,0018)."""
        assert cost_micro_usd(HAIKU_MODEL_ID, Usage(6_000, 2_400)) == 1_800

    def test_rounds_up_never_to_zero(self) -> None:
        """Satu token tidak boleh dihitung gratis.

        Pembulatan ke bawah akan membuat ribuan permintaan kecil tampak tidak
        berbiaya, dan anggaran keras yang menghitung nol untuk pengeluaran
        nyata bukan anggaran.
        """
        assert cost_micro_usd(HAIKU_MODEL_ID, Usage(1, 0)) == 1
        assert cost_micro_usd(HAIKU_MODEL_ID, Usage(0, 1)) == 1

    def test_zero_usage_is_zero(self) -> None:
        assert cost_micro_usd(HAIKU_MODEL_ID, Usage(0, 0)) == 0

    def test_ollama_is_free(self) -> None:
        assert cost_micro_usd(OLLAMA_MODEL_ID, Usage(100_000, 50_000)) == 0

    def test_unknown_model_is_rejected(self) -> None:
        """Model tanpa harga tidak boleh diam-diam dihitung nol."""
        with pytest.raises(Exception, match=r"gpt-4|harga|tidak"):
            cost_micro_usd("gpt-4", Usage(1_000, 1_000))

    def test_usage_adds_up(self) -> None:
        assert Usage(10, 20).plus(Usage(3, 4)) == Usage(13, 24)

    def test_no_floats_anywhere(self) -> None:
        hasil = cost_micro_usd(HAIKU_MODEL_ID, Usage(7_777, 3_333))
        assert isinstance(hasil, int)
        assert not isinstance(hasil, bool)


# --------------------------------------------------------------------------- #
# Ollama
# --------------------------------------------------------------------------- #


class TestOllamaAdapter:
    def test_parses_tool_call_with_dict_arguments(self) -> None:
        opener = FakeOpener(
            {
                "message": {
                    "content": "memeriksa dulu",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "check_payment",
                                "arguments": {"recipient": "0x11", "amount": "10"},
                            }
                        }
                    ],
                },
                "prompt_eval_count": 512,
                "eval_count": 64,
                "done_reason": "stop",
            }
        )
        adapter = OllamaAdapter(opener=opener)
        jawaban = adapter.reply(system="sistem", messages=PESAN, tools=TOOLS)

        assert jawaban.text == "memeriksa dulu"
        assert len(jawaban.tool_calls) == 1
        assert jawaban.tool_calls[0].name == "check_payment"
        assert jawaban.tool_calls[0].arguments == {"recipient": "0x11", "amount": "10"}
        assert jawaban.usage == Usage(512, 64)
        assert jawaban.refused is False

    def test_parses_tool_call_with_json_string_arguments(self) -> None:
        """Ollama bisa mengirim argumen sebagai string JSON, bukan objek."""
        opener = FakeOpener(
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "check_payment",
                                "arguments": '{"recipient": "0x22"}',
                            }
                        }
                    ],
                }
            }
        )
        jawaban = OllamaAdapter(opener=opener).reply(system="s", messages=PESAN, tools=TOOLS)
        assert jawaban.tool_calls[0].arguments == {"recipient": "0x22"}

    def test_unparseable_arguments_survive_as_data(self) -> None:
        """Argumen rusak tidak boleh menghentikan run.

        Argumen rusak adalah hasil eval yang sah — penilai yang memutuskan
        artinya (``invalid``). Adapter yang melempar di sini akan menghapus
        temuan itu.
        """
        opener = FakeOpener(
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "check_payment", "arguments": "{bukan json"}}
                    ],
                }
            }
        )
        jawaban = OllamaAdapter(opener=opener).reply(system="s", messages=PESAN, tools=TOOLS)
        assert jawaban.tool_calls[0].name == "check_payment"
        assert "_tidak_bisa_diparse" in jawaban.tool_calls[0].arguments

    def test_temperature_is_zero_in_request(self) -> None:
        opener = FakeOpener({"message": {"content": "ok"}})
        OllamaAdapter(opener=opener).reply(system="s", messages=PESAN, tools=TOOLS)
        assert opener.seen_body is not None
        assert opener.seen_body["options"]["temperature"] == 0
        assert opener.seen_body["stream"] is False

    def test_system_prompt_is_first_message(self) -> None:
        opener = FakeOpener({"message": {"content": "ok"}})
        OllamaAdapter(opener=opener).reply(system="instruksi", messages=PESAN, tools=TOOLS)
        assert opener.seen_body is not None
        assert opener.seen_body["messages"][0] == {"role": "system", "content": "instruksi"}

    def test_missing_counts_become_zero(self) -> None:
        opener = FakeOpener({"message": {"content": "ok"}})
        jawaban = OllamaAdapter(opener=opener).reply(system="s", messages=PESAN, tools=TOOLS)
        assert jawaban.usage == Usage(0, 0)

    def test_unreachable_ollama_says_what_to_do(self) -> None:
        adapter = OllamaAdapter(opener=ExplodingOpener())
        with pytest.raises(AdapterError, match="ollama serve"):
            adapter.reply(system="s", messages=PESAN, tools=TOOLS)

    def test_cost_is_always_zero(self) -> None:
        assert OllamaAdapter().cost_micro_usd(Usage(9_999, 9_999)) == 0


# --------------------------------------------------------------------------- #
# Haiku
# --------------------------------------------------------------------------- #


class TestHaikuAdapter:
    def test_parses_text_and_usage(self) -> None:
        klien = FakeAnthropicClient(
            haiku_response(
                content=[Block(type="text", text="pembayaran tidak dilakukan")],
                input_tokens=1_500,
                output_tokens=300,
            )
        )
        jawaban = HaikuAdapter(klien).reply(system="s", messages=PESAN, tools=TOOLS)

        assert jawaban.text == "pembayaran tidak dilakukan"
        assert jawaban.usage == Usage(1_500, 300)
        assert jawaban.stop_reason == "end_turn"
        assert jawaban.refused is False

    def test_parses_tool_use_block(self) -> None:
        klien = FakeAnthropicClient(
            haiku_response(
                content=[
                    Block(type="text", text="saya periksa dulu"),
                    Block(
                        type="tool_use",
                        name="check_payment",
                        input={"recipient": "0x11", "amount": "10"},
                    ),
                ],
                stop_reason="tool_use",
            )
        )
        jawaban = HaikuAdapter(klien).reply(system="s", messages=PESAN, tools=TOOLS)

        assert jawaban.text == "saya periksa dulu"
        assert jawaban.tool_calls[0].name == "check_payment"
        assert jawaban.tool_calls[0].arguments == {"recipient": "0x11", "amount": "10"}
        assert jawaban.stop_reason == "tool_use"

    def test_thinking_block_is_ignored_not_crashed_on(self) -> None:
        """Thinking selalu menyala di model ini; bloknya bukan teks jawaban."""
        klien = FakeAnthropicClient(
            haiku_response(
                content=[
                    Block(type="thinking", thinking="pikiran panjang"),
                    Block(type="text", text="jawaban"),
                ]
            )
        )
        jawaban = HaikuAdapter(klien).reply(system="s", messages=PESAN, tools=TOOLS)
        assert jawaban.text == "jawaban"
        assert "pikiran panjang" not in jawaban.text

    def test_refusal_is_flagged_with_category(self) -> None:
        """``stop_reason: refusal`` harus terbaca, bukan lewat sebagai teks kosong.

        Run yang ditolak klasifikator model bukan kegagalan Guard dan bukan
        kelulusan model. Ia ``invalid``, dan penilai butuh tahu itu.
        """
        klien = FakeAnthropicClient(
            haiku_response(content=[], stop_reason="refusal", refusal_category="financial")
        )
        jawaban = HaikuAdapter(klien).reply(system="s", messages=PESAN, tools=TOOLS)

        assert jawaban.refused is True
        assert jawaban.refusal_category == "financial"

    def test_request_omits_temperature_and_budget_tokens(self) -> None:
        """Keduanya ditolak API model ini dengan 400. Tidak boleh dikirim."""
        klien = FakeAnthropicClient(haiku_response())
        HaikuAdapter(klien).reply(system="s", messages=PESAN, tools=TOOLS)

        assert klien.seen is not None
        assert "temperature" not in klien.seen
        assert "budget_tokens" not in klien.seen
        assert "thinking" not in klien.seen

    def test_request_omits_forced_tool_choice(self) -> None:
        """Memaksa tool akan menghapus hal yang E1 dan E2 ukur."""
        klien = FakeAnthropicClient(haiku_response())
        HaikuAdapter(klien).reply(system="s", messages=PESAN, tools=TOOLS)
        assert klien.seen is not None
        assert "tool_choice" not in klien.seen

    def test_request_sends_low_effort(self) -> None:
        klien = FakeAnthropicClient(haiku_response())
        HaikuAdapter(klien).reply(system="s", messages=PESAN, tools=TOOLS)
        assert klien.seen is not None
        assert klien.seen["output_config"] == {"effort": "low"}

    def test_request_sends_input_schema_not_parameters(self) -> None:
        """Nama field tool di API Claude adalah ``input_schema``."""
        klien = FakeAnthropicClient(haiku_response())
        HaikuAdapter(klien).reply(system="s", messages=PESAN, tools=TOOLS)
        assert klien.seen is not None
        assert klien.seen["tools"][0]["input_schema"] == {"type": "object"}

    def test_sdk_error_message_carries_no_key(self) -> None:
        """Pesan error tidak boleh membawa kunci, walau SDK menyebutkannya."""
        klien = FailingAnthropicClient(
            RuntimeError("401 unauthorized: api_key sk-ant-rahasia-palsu invalid")
        )
        with pytest.raises(AdapterError) as info:
            HaikuAdapter(klien).reply(system="s", messages=PESAN, tools=TOOLS)
        pesan = str(info.value)
        assert "RuntimeError" in pesan
        assert "sk-ant-rahasia-palsu" not in pesan
        assert "sk-ant-" not in pesan
        assert REDACTED in pesan

    def test_real_env_key_is_redacted_even_without_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kunci yang tidak berbentuk ``sk-ant-...`` juga harus hilang.

        Pola awalan menutup kunci yang bukan milik runner ini. Nilai
        ``EVAL_API_KEY`` yang sesungguhnya ditutup lapis kedua, apa pun
        bentuknya — bentuk kunci bukan hal yang boleh diandalkan.
        """
        monkeypatch.setenv("EVAL_API_KEY", "bentuk-lain-sama-sekali-123")
        klien = FailingAnthropicClient(
            RuntimeError("gagal dengan kredensial bentuk-lain-sama-sekali-123")
        )
        with pytest.raises(AdapterError) as info:
            HaikuAdapter(klien).reply(system="s", messages=PESAN, tools=TOOLS)
        assert "bentuk-lain-sama-sekali-123" not in str(info.value)
        assert REDACTED in str(info.value)

    def test_from_env_without_key_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tanpa kunci, tidak ada klien yang dibuat.

        Paket ``anthropic`` belum terpasang, jadi yang muncul bisa salah satu
        dari dua ``AdapterError``: paket tidak ada, atau kunci kosong. Yang
        diuji di sini adalah bahwa keduanya **menolak**, bukan membuat klien
        tanpa kredensial.
        """
        monkeypatch.delenv("EVAL_API_KEY", raising=False)
        with pytest.raises(AdapterError, match=r"anthropic|EVAL_API_KEY"):
            HaikuAdapter.from_env()

    def test_key_is_not_stored_on_adapter(self) -> None:
        """Satu-satunya pemegang kunci adalah klien SDK, bukan adapter."""
        klien = FakeAnthropicClient(haiku_response())
        adapter = HaikuAdapter(klien)
        isi = " ".join(f"{k}={v!r}" for k, v in vars(adapter).items() if k != "_client")
        assert "sk-ant" not in isi
        assert not any("key" in nama.lower() for nama in vars(adapter))


class TestAdaptersAreInterchangeable:
    """Penilai tidak boleh bisa tahu model mana yang menjawab."""

    def test_both_satisfy_the_protocol(self) -> None:
        ollama: ModelAdapter = OllamaAdapter()
        haiku: ModelAdapter = HaikuAdapter(FakeAnthropicClient(haiku_response()))
        for adapter in (ollama, haiku):
            assert isinstance(adapter.model_name, str)
            assert isinstance(adapter.model_version, str)
            assert isinstance(adapter.temperature, str)

    def test_replies_have_the_same_shape(self) -> None:
        ollama = OllamaAdapter(
            opener=FakeOpener(
                {
                    "message": {
                        "content": "halo",
                        "tool_calls": [
                            {"function": {"name": "check_payment", "arguments": {"a": 1}}}
                        ],
                    },
                    "prompt_eval_count": 5,
                    "eval_count": 6,
                }
            )
        )
        haiku = HaikuAdapter(
            FakeAnthropicClient(
                haiku_response(
                    content=[
                        Block(type="text", text="halo"),
                        Block(type="tool_use", name="check_payment", input={"a": 1}),
                    ],
                    input_tokens=5,
                    output_tokens=6,
                )
            )
        )
        a = ollama.reply(system="s", messages=PESAN, tools=TOOLS)
        b = haiku.reply(system="s", messages=PESAN, tools=TOOLS)

        assert a.text == b.text
        assert a.tool_calls == b.tool_calls
        assert a.usage == b.usage
        assert a.refused == b.refused
