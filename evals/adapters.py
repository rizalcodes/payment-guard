"""Adapter model netral: antarmuka yang sama untuk Ollama dan API Claude.

PRD v2.1 bagian 12: "Adapter model netral: antarmuka sama untuk Ollama dan
API." Yang dinetralkan bukan hanya nama method, tapi juga **bentuk jawaban**:
:class:`ModelReply` sama untuk keduanya, sehingga penilai di
:mod:`evals.scoring` tidak pernah tahu model mana yang menjawab.

Harga dan ID model diverifikasi ke dokumentasi resmi sebelum M8, bukan dari
ingatan (PRD v2.1 bagian 12, poin terakhir). Hasil verifikasinya ada di
:data:`HAIKU_MODEL_ID` dan :data:`PRICE_MICRO_USD_PER_MTOK`.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

# --------------------------------------------------------------------------- #
# Model dan harga, terverifikasi
# --------------------------------------------------------------------------- #

#: ID model API. Diverifikasi 9 Nov 2026 ke dokumentasi resmi Claude.
#:
#: Tanpa sufiks tanggal: ID itu lengkap apa adanya.
HAIKU_MODEL_ID: Final = "claude-haiku-5-5"

#: ID model lokal lewat Ollama.
OLLAMA_MODEL_ID: Final = "qwen3:1.7b"

#: Harga dalam **micro-USD per juta token**, terverifikasi 9 Nov 2026.
#:
#: $0,10/Mtok input dan $0,50/Mtok output menjadi 100_000 dan 500_000
#: micro-USD. Bilangan bulat, bukan ``float``: uang tidak pernah lewat
#: ``float`` di repo ini, dan anggaran yang salah 0,0001 USD karena pembulatan
#: biner adalah anggaran yang tidak bisa dipertanggungjawabkan.
#:
#: **Catatan harga yang perlu diketahui:** angka ini berlaku untuk prompt
#: sampai 100K token. Di atasnya harganya $0,50/$2,50 per Mtok. Prompt eval ini
#: sekitar 1-2K token, jadi batas itu tidak pernah tersentuh — tapi kalau suatu
#: hari skenarionya tumbuh, harga ini berhenti benar.
PRICE_MICRO_USD_PER_MTOK: Final[Mapping[str, Mapping[str, int]]] = {
    HAIKU_MODEL_ID: {"input": 100_000, "output": 500_000},
    OLLAMA_MODEL_ID: {"input": 0, "output": 0},
}

#: Satu juta, sebagai pembagi harga. Dinamai supaya rumusnya terbaca.
TOKENS_PER_MTOK: Final = 1_000_000

#: ``effort`` untuk model API.
#:
#: ``low`` dengan sengaja. Di Claude Haiku 5.5 thinking **selalu menyala**
#: (``budget_tokens`` ditolak dengan 400) dan token thinking ditagih sebagai
#: output, jadi ``effort`` adalah satu-satunya kendali biaya yang ada. Default
#: modelnya ``medium``; eval ini tidak butuh kedalaman penalaran, ia butuh
#: jawaban yang bisa diulang 10 kali dengan biaya yang terduga.
HAIKU_EFFORT: Final = "low"

#: ``max_tokens`` per permintaan. Cukup untuk satu putaran tool call plus
#: penjelasan singkat, dan cukup kecil untuk membatasi biaya satu run yang
#: kabur.
HAIKU_MAX_TOKENS: Final = 2048

#: Temperature yang dilaporkan untuk model API.
#:
#: Bukan angka, karena **tidak bisa disetel**: Claude Haiku 5.5 menolak nilai
#: ``temperature`` non-default dengan 400, dan sejak API version 2026-10-21
#: parameter itu ditolak di semua model. Metadata hasil menyebut ini apa adanya
#: alih-alih mengarang "0" yang tidak pernah dikirim.
HAIKU_TEMPERATURE_NOTE: Final = "default (ditolak API)"

#: Temperature untuk Ollama. Di sini angka 0 memang dikirim dan dipatuhi.
OLLAMA_TEMPERATURE: Final = "0"

#: Endpoint Ollama lokal. Loopback saja.
OLLAMA_URL: Final = "http://127.0.0.1:11434/api/chat"

#: Batas waktu **satu** permintaan Ollama, dalam detik.
#:
#: Diukur, bukan ditebak. Satu putaran E1 di mesin ini memakan 116,5 detik:
#: 576 token prompt, 677 token output, karena Qwen3 berpikir secara bawaan dan
#: token pikirannya ikut dihasilkan. Nilai pertama di berkas ini adalah 120,
#: dan ia gagal dengan selisih tiga detik.
#:
#: 600 memberi ruang untuk putaran-putaran berikutnya, yang konteksnya lebih
#: panjang dan karena itu lebih lambat. Batas ini **bukan** anggaran waktu satu
#: run: satu run bisa enam putaran. Ia hanya membedakan "lambat" dari "mati".
OLLAMA_TIMEOUT_SECONDS: Final = 600


class AdapterError(RuntimeError):
    """Adapter gagal menjawab.

    Pesannya ditampilkan ke operator, jadi ia tidak pernah memuat kunci API.
    """


#: Bentuk token kunci API Claude. Dipakai untuk menyunting, bukan memvalidasi.
API_KEY_PATTERN: Final = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")

#: Pengganti yang ditulis di tempat kunci.
REDACTED: Final = "[kunci disunting]"


def redact(text: str) -> str:
    """Hapus apa pun yang menyerupai kunci API dari ``text``.

    Dipakai pada tiap pesan error yang berasal dari SDK. Pesan SDK bisa
    mengutip permintaan yang gagal, dan permintaan itu membawa kunci; tanpa
    penyuntingan, kunci itu akan berakhir di ``summary.md`` atau di terminal
    operator. Meneruskan "hanya baris pertama" **tidak** cukup: tidak ada
    jaminan kunci tidak ada di baris pertama. Sebuah tes memasang kunci
    tiruan di pesan pengecualian dan menuntut ia hilang.

    Dua lapis, karena masing-masing menutup yang tidak tertutup yang lain:

    * nilai ``EVAL_API_KEY`` yang sesungguhnya, apa pun bentuknya;
    * pola ``sk-ant-...``, untuk kunci yang bukan milik runner ini — kunci
      salah ketik, kunci dari pesan upstream, kunci yang dikutip SDK.
    """
    kunci = (os.environ.get(EVAL_API_KEY_VAR) or "").strip()
    if kunci:
        text = text.replace(kunci, REDACTED)
    return API_KEY_PATTERN.sub(REDACTED, text)


# --------------------------------------------------------------------------- #
# Bentuk jawaban yang netral
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Usage:
    """Token yang dipakai satu permintaan."""

    input_tokens: int
    output_tokens: int

    def plus(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True)
class ToolCall:
    """Satu permintaan tool dari model.

    ``arguments`` apa adanya dari model, **belum** divalidasi: argumen yang
    rusak adalah hasil eval yang sah (``invalid``), bukan kecelakaan harness.
    """

    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolSpec:
    """Satu tool yang ditawarkan ke model."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class Turn:
    """Satu pesan dalam riwayat percakapan."""

    role: str
    content: Any


@dataclass(frozen=True)
class ModelReply:
    """Jawaban satu permintaan, dalam bentuk yang sama untuk semua model.

    ``raw`` disimpan ke transkrip apa adanya. Ia **tidak pernah** memuat kunci
    API: yang masuk ke sini adalah jawaban server, bukan permintaannya.
    """

    text: str
    tool_calls: tuple[ToolCall, ...]
    usage: Usage
    stop_reason: str
    raw: dict[str, Any] = field(default_factory=dict)
    #: Kategori penolakan dari ``stop_details`` bila ada. ``None`` untuk
    #: jawaban biasa.
    refusal_category: str | None = None

    @property
    def refused(self) -> bool:
        """Model menolak lewat klasifikator keamanannya sendiri.

        Berbeda dari agent yang **memilih** tidak membayar: yang pertama
        kegagalan harness untuk mengukur apa pun (``invalid``), yang kedua
        justru hasil yang dicari (``pass``).
        """
        return self.stop_reason == "refusal"


def cost_micro_usd(model: str, usage: Usage) -> int:
    """Biaya satu permintaan dalam micro-USD, **dibulatkan ke atas**.

    Dibulatkan ke atas dengan sengaja. Pembulatan ini dipakai penjaga
    anggaran, dan anggaran yang membulatkan ke bawah akan selalu sedikit
    melebihi batasnya — kesalahan yang arahnya salah. Satu run yang biayanya
    nyata 0,4 micro-USD dihitung 1.

    Raises:
        AdapterError: bila harga model itu tidak diketahui. Menebak harga lebih
            buruk daripada berhenti: anggaran yang dihitung dari harga yang
            salah bukan anggaran.
    """
    harga = PRICE_MICRO_USD_PER_MTOK.get(model)
    if harga is None:
        raise AdapterError(
            f"harga model {model!r} tidak diketahui; tambahkan ke "
            "PRICE_MICRO_USD_PER_MTOK setelah memeriksa dokumentasi resmi"
        )
    total = usage.input_tokens * harga["input"] + usage.output_tokens * harga["output"]
    # Pembagian pembulatan-ke-atas tanpa float: -(-a // b).
    return -(-total // TOKENS_PER_MTOK)


class ModelAdapter(Protocol):
    """Antarmuka yang sama untuk model lokal dan model API."""

    model_name: str
    model_version: str
    temperature: str

    def reply(
        self,
        *,
        system: str,
        messages: Sequence[Turn],
        tools: Sequence[ToolSpec],
    ) -> ModelReply: ...

    def cost_micro_usd(self, usage: Usage) -> int: ...


# --------------------------------------------------------------------------- #
# Ollama
# --------------------------------------------------------------------------- #


def _as_dict(value: object) -> dict[str, Any]:
    """Jawaban server sebagai dict yang bisa ditulis ke transkrip.

    SDK resmi mengembalikan objek Pydantic; tiruan di tes mengembalikan objek
    biasa. Keduanya harus bisa masuk JSON tanpa harness tahu bedanya.
    """
    for nama in ("to_dict", "model_dump"):
        metode = getattr(value, nama, None)
        if callable(metode):
            hasil = metode()
            if isinstance(hasil, dict):
                return dict(hasil)
    if isinstance(value, dict):
        return dict(value)
    return {"repr": repr(value)}


class OllamaAdapter:
    """Model lokal lewat Ollama. Biaya nol, temperature benar-benar 0."""

    def __init__(
        self,
        *,
        model: str = OLLAMA_MODEL_ID,
        url: str = OLLAMA_URL,
        timeout_seconds: int = OLLAMA_TIMEOUT_SECONDS,
        opener: Any | None = None,
    ) -> None:
        self.model_name = model
        self.model_version = model
        self.temperature = OLLAMA_TEMPERATURE
        self._url = url
        self._timeout = timeout_seconds
        # ``opener`` disuntik tes supaya tidak ada HTTP sungguhan di tes unit.
        self._opener = opener or urllib.request.urlopen

    def cost_micro_usd(self, usage: Usage) -> int:
        return cost_micro_usd(self.model_name, usage)

    def _request_body(
        self, system: str, messages: Sequence[Turn], tools: Sequence[ToolSpec]
    ) -> dict[str, Any]:
        isi: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for turn in messages:
            isi.append({"role": turn.role, "content": turn.content})
        return {
            "model": self.model_name,
            "messages": isi,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.parameters,
                    },
                }
                for spec in tools
            ],
            "stream": False,
            "options": {"temperature": 0},
        }

    def reply(
        self,
        *,
        system: str,
        messages: Sequence[Turn],
        tools: Sequence[ToolSpec],
    ) -> ModelReply:
        body = json.dumps(self._request_body(system, messages, tools)).encode("utf-8")
        permintaan = urllib.request.Request(  # noqa: S310 - URL konstanta loopback
            self._url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with self._opener(permintaan, timeout=self._timeout) as jawaban:
                mentah = json.loads(jawaban.read().decode("utf-8"))
        except (urllib.error.URLError, OSError) as exc:
            raise AdapterError(
                f"Ollama di {self._url} tidak bisa dihubungi: {exc}. "
                "Jalankan `ollama serve` dan pastikan modelnya sudah di-pull."
            ) from None
        except json.JSONDecodeError as exc:
            raise AdapterError(f"jawaban Ollama bukan JSON: {exc}") from None
        return self._parse(mentah)

    def _parse(self, mentah: dict[str, Any]) -> ModelReply:
        pesan = mentah.get("message") or {}
        panggilan: list[ToolCall] = []
        for item in pesan.get("tool_calls") or []:
            fungsi = item.get("function") or {}
            argumen = fungsi.get("arguments")
            # Ollama bisa mengirim argumen sebagai dict atau sebagai string JSON.
            if isinstance(argumen, str):
                try:
                    argumen = json.loads(argumen)
                except json.JSONDecodeError:
                    argumen = {"_tidak_bisa_diparse": argumen}
            if not isinstance(argumen, dict):
                argumen = {"_bentuk_tak_terduga": repr(argumen)}
            panggilan.append(ToolCall(name=str(fungsi.get("name", "")), arguments=argumen))
        return ModelReply(
            text=str(pesan.get("content") or ""),
            tool_calls=tuple(panggilan),
            usage=Usage(
                input_tokens=int(mentah.get("prompt_eval_count") or 0),
                output_tokens=int(mentah.get("eval_count") or 0),
            ),
            stop_reason=str(mentah.get("done_reason") or "stop"),
            raw=mentah,
        )


# --------------------------------------------------------------------------- #
# Claude Haiku 5.5
# --------------------------------------------------------------------------- #


class HaikuAdapter:
    """Model API lewat SDK resmi ``anthropic``.

    Kliennya **disuntik**, dan itu bukan hanya demi tes. Paket ``anthropic``
    belum terpasang di mesin ini, dan Fase 2 M8 tidak boleh memanggil API
    sungguhan — jadi adapter ini bisa ditulis, diuji, dan dibaca tanpa paket
    itu ada. :meth:`from_env` yang mengimpornya, dan ia hanya dipanggil di
    jalur live run.

    Tiga hal yang **tidak** dikirim, masing-masing karena API menolaknya
    dengan 400 di model ini:

    * ``temperature`` — nilai non-default ditolak.
    * ``budget_tokens`` — thinking selalu menyala; kendalinya ``effort``.
    * ``tool_choice`` paksa — dan ini juga tidak boleh secara metodologis:
      E1 dan E2 menguji apakah agent **memilih** untuk tidak membayar.
      Memaksanya memanggil tool akan menghapus hal yang sedang diukur.
    """

    def __init__(self, client: Any, *, model: str = HAIKU_MODEL_ID) -> None:
        self.model_name = model
        self.model_version = model
        self.temperature = HAIKU_TEMPERATURE_NOTE
        self._client = client

    @classmethod
    def from_env(cls, *, model: str = HAIKU_MODEL_ID) -> HaikuAdapter:
        """Buat adapter dengan kunci dari ``EVAL_API_KEY``.

        Kunci dibaca di sini dan **tidak disimpan** di atribut mana pun:
        satu-satunya pemegangnya adalah klien SDK. Pesan error tidak pernah
        memuat nilainya.

        Raises:
            AdapterError: bila paket ``anthropic`` belum terpasang atau
                ``EVAL_API_KEY`` kosong.
        """
        import os

        try:
            import anthropic
        except ModuleNotFoundError:
            raise AdapterError(
                "paket `anthropic` belum terpasang. Live run ke API butuh "
                "`uv add --group eval anthropic`; tes unit tidak membutuhkannya."
            ) from None

        kunci = (os.environ.get(EVAL_API_KEY_VAR) or "").strip()
        if not kunci:
            raise AdapterError(
                f"{EVAL_API_KEY_VAR} kosong. Isi di .env, dan hanya di sana: "
                "tidak ada komponen selain eval runner yang membacanya."
            )
        return cls(anthropic.Anthropic(api_key=kunci), model=model)

    def cost_micro_usd(self, usage: Usage) -> int:
        return cost_micro_usd(self.model_name, usage)

    def reply(
        self,
        *,
        system: str,
        messages: Sequence[Turn],
        tools: Sequence[ToolSpec],
    ) -> ModelReply:
        try:
            jawaban = self._client.messages.create(
                model=self.model_name,
                max_tokens=HAIKU_MAX_TOKENS,
                output_config={"effort": HAIKU_EFFORT},
                system=system,
                messages=[{"role": t.role, "content": t.content} for t in messages],
                tools=[
                    {
                        "name": spec.name,
                        "description": spec.description,
                        "input_schema": spec.parameters,
                    }
                    for spec in tools
                ],
            )
        except Exception as exc:
            # Jenis pengecualian SDK tidak bisa disebut tanpa mengimpor paketnya,
            # dan paket itu sengaja tidak diimpor di tingkat modul. Yang penting
            # di sini: pesannya tidak boleh memuat kunci, jadi hanya tipe dan
            # baris pertamanya yang diteruskan.
            baris = redact(str(exc)).splitlines()
            raise AdapterError(
                f"permintaan ke {self.model_name} gagal "
                f"({type(exc).__name__}): {baris[0] if baris else 'tanpa pesan'}"
            ) from None
        return self._parse(jawaban)

    def _parse(self, jawaban: Any) -> ModelReply:
        teks: list[str] = []
        panggilan: list[ToolCall] = []
        for blok in jawaban.content or []:
            jenis = getattr(blok, "type", None)
            if jenis == "text":
                teks.append(str(getattr(blok, "text", "")))
            elif jenis == "tool_use":
                masukan = getattr(blok, "input", None)
                panggilan.append(
                    ToolCall(
                        name=str(getattr(blok, "name", "")),
                        arguments=dict(masukan) if isinstance(masukan, dict) else {},
                    )
                )

        pemakaian = getattr(jawaban, "usage", None)
        usage = Usage(
            input_tokens=int(getattr(pemakaian, "input_tokens", 0) or 0),
            output_tokens=int(getattr(pemakaian, "output_tokens", 0) or 0),
        )

        stop_reason = str(getattr(jawaban, "stop_reason", "") or "")
        detail = getattr(jawaban, "stop_details", None)
        kategori = getattr(detail, "category", None) if detail is not None else None

        return ModelReply(
            text="\n".join(teks),
            tool_calls=tuple(panggilan),
            usage=usage,
            stop_reason=stop_reason,
            raw=_as_dict(jawaban),
            refusal_category=str(kategori) if kategori is not None else None,
        )


#: Nama variabel environment yang memuat kunci API eval.
#:
#: Didefinisikan di sini, di dalam ``evals/``, dan **hanya** di sini.
#: Arsitektur v1.1 bagian 2.1 sudah mencatatnya sebagai rahasia yang dibaca
#: eval runner. Ada tes yang memeriksa nama ini tidak muncul di
#: ``src/payment_guard/`` mana pun.
EVAL_API_KEY_VAR: Final = "EVAL_API_KEY"


__all__ = [
    "API_KEY_PATTERN",
    "EVAL_API_KEY_VAR",
    "HAIKU_EFFORT",
    "HAIKU_MAX_TOKENS",
    "HAIKU_MODEL_ID",
    "HAIKU_TEMPERATURE_NOTE",
    "OLLAMA_MODEL_ID",
    "OLLAMA_TEMPERATURE",
    "OLLAMA_URL",
    "PRICE_MICRO_USD_PER_MTOK",
    "REDACTED",
    "AdapterError",
    "HaikuAdapter",
    "ModelAdapter",
    "ModelReply",
    "OllamaAdapter",
    "ToolCall",
    "ToolSpec",
    "Turn",
    "Usage",
    "cost_micro_usd",
    "redact",
]
