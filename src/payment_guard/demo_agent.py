"""Agent demo: klien MCP dengan tepat tiga tool, tanpa LLM.

PRD v2.1 bagian 4, PRD v2 bagian 14 (M4).

Agent ini sengaja **deterministik**. Model bahasa menyusul di M8; yang perlu
dibuktikan sekarang bukan bahwa model bisa dibujuk, melainkan bahwa permukaan
yang tersedia bagi agent memang hanya tiga tool dan tidak ada jalan keluar dari
situ. Skrip yang jalurnya tetap membuat pembuktian itu bisa diuji.

Tiga tool, dan tidak lebih
--------------------------

``check_payment`` dan ``get_decision_status`` datang dari server Guard lewat
stdio MCP. ``execute_payment`` **bukan** tool Guard: ia pembungkus lokal di
host agent (PRD v2.1 nomor 2), supaya Guard tidak pernah memegang kunci wallet
dan tidak pernah bisa menggerakkan uang sendiri.

Tidak ada tool shell, berkas, maupun browsing. Daftar :data:`TOOL_NAMES`
tertutup dan ada tes yang membandingkannya dengan apa yang benar-benar
didaftarkan.

``execute_payment`` di M4 hanyalah stub
---------------------------------------

Ia memverifikasi envelope dengan **public key saja**, lalu berhenti. Tidak
membangun transaksi, tidak menyentuh kunci wallet, tidak menghubungi chain.
Itu M5. Yang diuji di sini adalah bahwa agent tidak bisa membelanjakan apa pun
hanya dengan memegang jawaban Guard: envelope harus lolos tanda tangan dan
``mandate_hash``-nya harus yang diharapkan.

``agent_id`` tidak pernah dikirim
---------------------------------

Tidak ada argumen tool yang memuatnya. Identitas datang dari mandate yang
dimuat Guard saat start (Security v1.1 S1, ST-01).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from payment_guard.signing import (
    EnvelopeError,
    VerificationError,
    parse_envelope,
    parse_public_keys,
    verify,
)

#: Nama tool yang dimiliki agent. Daftar tertutup.
#:
#: Dua yang pertama dari Guard, yang ketiga lokal. Tidak ada shell, berkas,
#: atau browsing: agent yang bisa membaca berkas bisa membaca kunci, dan agent
#: yang bisa menjalankan perintah tidak lagi dibatasi tool mana pun.
TOOL_NAMES: Final[tuple[str, ...]] = (
    "check_payment",
    "get_decision_status",
    "execute_payment",
)

#: Tool yang dilayani server Guard lewat stdio.
GUARD_TOOL_NAMES: Final[tuple[str, ...]] = ("check_payment", "get_decision_status")

#: Tool lokal di host agent.
LOCAL_TOOL_NAMES: Final[tuple[str, ...]] = ("execute_payment",)

#: Nama argumen tiap tool. ``agent_id`` tidak ada di mana pun, dengan sengaja.
TOOL_ARGUMENTS: Final[dict[str, tuple[str, ...]]] = {
    "check_payment": ("recipient", "amount", "chain", "reason"),
    "get_decision_status": ("decision_id",),
    "execute_payment": ("signed_decision", "expected_mandate_hash"),
}

#: Perintah untuk menjalankan server Guard sebagai subproses stdio.
GUARD_COMMAND: Final[tuple[str, ...]] = ("guard", "serve")


class DemoAgentError(RuntimeError):
    """Agent demo berhenti. Pesannya untuk operator yang menjalankan demo."""


@dataclass(frozen=True)
class ExecutionAttempt:
    """Hasil stub ``execute_payment``.

    ``broadcast`` selalu False di M4: tidak ada transaksi yang dibangun dan
    tidak ada chain yang dihubungi. Field-nya ada supaya bentuk jawabannya
    tidak berubah saat Executor sungguhan datang di M5.
    """

    verified: bool
    detail: str
    decision_id: str | None = None
    amount_base: str | None = None
    recipient: str | None = None
    broadcast: bool = False


def tool_names() -> tuple[str, ...]:
    """Nama tool yang dimiliki agent ini."""
    return TOOL_NAMES


def execute_payment(
    signed_decision: object,
    expected_mandate_hash: str,
    *,
    public_keys_path: Path,
) -> ExecutionAttempt:
    """Stub Executor: verifikasi envelope dengan public key saja.

    Dua pemeriksaan, dan keduanya perlu. Tanda tangan membuktikan Guard yang
    mengeluarkannya; ``mandate_hash`` membuktikan ia dikeluarkan di bawah
    aturan yang kita harapkan. Envelope yang sah atas mandate yang salah tetap
    envelope yang salah (Arsitektur v1.1 bagian 7, cek 4b).

    Tidak ada kunci rahasia yang dibaca di sini, dan tidak ada jaringan.

    Args:
        signed_decision: envelope dari jawaban Guard, apa adanya.
        expected_mandate_hash: hash mandate yang agent yakini sedang berlaku.
        public_keys_path: registri kunci publik, mis. ``guard_public.json``.

    Returns:
        Hasil verifikasi. Tidak pernah melempar untuk envelope yang buruk:
        "ditolak" adalah jawaban yang sah, bukan kecelakaan.
    """
    if signed_decision is None:
        return ExecutionAttempt(False, "tidak ada signed_decision untuk dieksekusi")

    registry = parse_public_keys(public_keys_path.read_text(encoding="utf-8"))
    try:
        envelope = parse_envelope(json.dumps(signed_decision))
        payload = verify(envelope, registry)
    except (EnvelopeError, VerificationError) as exc:
        return ExecutionAttempt(False, f"envelope ditolak: {exc}")

    if payload.mandate_hash != expected_mandate_hash:
        return ExecutionAttempt(
            False,
            "envelope sah tapi mandate_hash-nya bukan yang diharapkan; "
            "aturan yang dipakai Guard bukan aturan yang agent kira",
        )

    return ExecutionAttempt(
        verified=True,
        detail="envelope terverifikasi; M4 berhenti di sini (tanpa chain, tanpa kunci wallet)",
        decision_id=payload.decision_id,
        amount_base=payload.amount_base,
        recipient=payload.recipient,
        broadcast=False,
    )


def _text_of(result: Any) -> dict[str, Any]:
    """Ambil satu blok teks JSON dari jawaban tool MCP."""
    blocks = list(result.content)
    if len(blocks) != 1:
        raise DemoAgentError(f"jawaban tool tidak berbentuk satu blok: {len(blocks)} blok")
    return json.loads(blocks[0].text)


async def run_once(
    recipient: str,
    amount: str,
    reason: str,
    *,
    chain: str = "base-sepolia",
    env_file: Path | None = None,
    public_keys_path: Path,
    expected_mandate_hash: str | None = None,
) -> dict[str, Any]:
    """Jalankan satu putaran: minta izin, baca status, lalu coba eksekusi.

    Jalurnya tetap dan tidak bercabang pada isi ``reason``: teks itu data, dan
    agent ini tidak punya tempat untuk menafsirkannya.
    """
    args = list(GUARD_COMMAND[1:])
    if env_file is not None:
        args += ["--env-file", str(env_file)]
    parameters = StdioServerParameters(command=GUARD_COMMAND[0], args=args)

    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tersedia = tuple(tool.name for tool in (await session.list_tools()).tools)
            if set(tersedia) != set(GUARD_TOOL_NAMES):
                raise DemoAgentError(f"server menawarkan tool tak terduga: {sorted(tersedia)}")

            decision = _text_of(
                await session.call_tool(
                    "check_payment",
                    {
                        "recipient": recipient,
                        "amount": amount,
                        "chain": chain,
                        "reason": reason,
                    },
                )
            )
            status = _text_of(
                await session.call_tool(
                    "get_decision_status", {"decision_id": decision["decision_id"]}
                )
            )

    mandate_hash = expected_mandate_hash or decision["mandate_hash"]
    attempt = execute_payment(
        status.get("signed_decision"),
        mandate_hash,
        public_keys_path=public_keys_path,
    )
    return {
        "decision": decision,
        "status": status,
        "execution": {
            "verified": attempt.verified,
            "detail": attempt.detail,
            "broadcast": attempt.broadcast,
        },
    }


def main(argv: list[str] | None = None) -> int:
    """Jalankan satu putaran demo dan cetak hasilnya sebagai JSON."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="payment-guard-demo-agent",
        description="Agent demo deterministik dengan tiga tool. Tanpa LLM (M4).",
    )
    parser.add_argument("recipient")
    parser.add_argument("amount")
    parser.add_argument("--reason", default="demo M4")
    parser.add_argument("--chain", default="base-sepolia")
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--public-keys-file", type=Path, default=Path("guard_public.json"))
    # ``None`` diteruskan apa adanya: argparse membacanya sebagai
    # ``sys.argv[1:]``. Versi pertama mengubahnya menjadi ``[]``, yang membuat
    # setiap argumen dari baris perintah diabaikan dan perintahnya selalu gagal
    # pada dua argumen wajibnya. Tidak ada tes yang memanggil ``main`` saat itu,
    # jadi tidak ada yang menangkapnya.
    args = parser.parse_args(argv)

    hasil = asyncio.run(
        run_once(
            args.recipient,
            args.amount,
            args.reason,
            chain=args.chain,
            env_file=args.env_file,
            public_keys_path=args.public_keys_file,
        )
    )
    print(json.dumps(hasil, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
