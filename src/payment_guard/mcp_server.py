"""MCP server Guard: tepat dua tool, transport stdio.

PRD v2.1 bagian 4 dan 7, Security v1.1 S1, E1, ST-01.

Guard mengekspos **dua** tool dan tidak lebih:

``check_payment(recipient, amount, chain, reason)``
    Menilai satu permintaan terhadap mandate aktif dan mencatat keputusannya.

``get_decision_status(decision_id)``
    Membaca keputusan milik agent ini saja.

``execute_payment`` **bukan** tool Guard. Ia pembungkus lokal di host agent
(PRD v2.1 nomor 2 di changelog), jadi Guard tidak pernah memegang kunci wallet
dan tidak pernah bisa menggerakkan uang sendiri.

Tidak ada ``agent_id`` di skema tool
------------------------------------

Identitas agent datang dari mandate yang dimuat saat start, bukan dari
argumen. Jaminannya bukan validasi tapi bentuk: tidak ada parameter untuk itu,
jadi tidak ada yang bisa mengirimkannya. Ada tes yang memeriksa skema yang
dibangun FastMCP, bukan hanya tanda tangan fungsinya.

Field ``reason``
----------------

``reason`` diterima, disimpan sebagai data di ``decisions.reason_text``, dan
berhenti di situ. Ia tidak pernah masuk ke evaluasi aturan, tidak pernah masuk
audit, dan tidak pernah masuk prompt model mana pun (PRD v2.1 bagian 3 nomor
10). Penampilannya ke operator adalah urusan M4 lewat ``render_untrusted``.

Fail-closed
-----------

Setiap tool memeriksa kesehatan sesi sebelum menyentuh database. Guard yang
kehilangan lock sesinya mengembalikan error, bukan keputusan.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

from mcp.server.fastmcp import FastMCP

from payment_guard.config import RUNTIME_ENV_FILENAME, GuardConfigError, load_guard_config
from payment_guard.decision_service import (
    CheckPaymentInput,
    InputError,
    check_payment,
    get_decision_status,
)
from payment_guard.guard_service import (
    GuardSession,
    GuardStartupError,
    GuardUnavailableError,
    Heartbeat,
    start_guard,
)

#: Nama server yang dilihat klien MCP.
SERVER_NAME: Final = "payment-guard"

#: Nama kedua tool. Daftar tertutup; ada tes yang membandingkannya dengan
#: skema yang dibangun FastMCP.
TOOL_NAMES: Final[tuple[str, ...]] = ("check_payment", "get_decision_status")

#: Jawaban untuk ``decision_id`` yang tidak ada **atau** milik agent lain.
#: Satu jawaban untuk dua keadaan dengan sengaja: membedakannya akan memberi
#: agent cara menghitung keputusan agent lain.
NOT_FOUND_RESPONSE: Final[dict[str, object]] = {
    "found": False,
    "detail": "decision_id tidak ditemukan untuk agent ini",
}


class ToolError(RuntimeError):
    """Error yang dikembalikan ke agent sebagai kegagalan tool.

    Bukan keputusan: tidak ada baris ``decisions`` yang ditulis. Pesannya
    ditujukan untuk agent dan tidak pernah memuat rahasia.
    """


def build_server(session: GuardSession) -> FastMCP:
    """Susun server MCP untuk satu sesi Guard.

    Dipisah dari :func:`main` supaya tes bisa memeriksa skema tool dan
    memanggil tool-nya tanpa menjalankan transport stdio.
    """
    server = FastMCP(SERVER_NAME)

    @server.tool(name="check_payment")
    def _check_payment(recipient: str, amount: str, chain: str, reason: str) -> dict:
        """Minta izin membayar sebelum membayar.

        Args:
            recipient: alamat tujuan, ``0x`` + 40 hex.
            amount: jumlah USDC desimal, mis. ``"25.5"``. Maksimum 6 desimal.
            chain: nama chain, harus ``"base-sepolia"``.
            reason: catatan untuk operator, maksimum 500 karakter. Disimpan
                sebagai data; tidak mempengaruhi keputusan.
        """
        try:
            result = check_payment(
                session,
                CheckPaymentInput(recipient=recipient, amount=amount, chain=chain, reason=reason),
            )
        except InputError as exc:
            raise ToolError(str(exc)) from None
        except GuardUnavailableError as exc:
            raise ToolError(str(exc)) from None
        return result.as_tool_response()

    @server.tool(name="get_decision_status")
    def _get_decision_status(decision_id: str) -> dict:
        """Baca keputusan yang sudah dibuat untuk agent ini.

        Args:
            decision_id: UUID yang dikembalikan ``check_payment``.
        """
        try:
            result = get_decision_status(session, decision_id)
        except InputError as exc:
            raise ToolError(str(exc)) from None
        except GuardUnavailableError as exc:
            raise ToolError(str(exc)) from None
        if result is None:
            return dict(NOT_FOUND_RESPONSE)
        return {"found": True, **result.as_tool_response()}

    # Nama tool ditetapkan eksplisit lewat dekoratornya. Fungsi Python-nya
    # diberi awalan garis bawah karena namanya akan menutupi fungsi yang
    # diimpor dari decision_service.
    return server


async def tool_names(server: FastMCP) -> tuple[str, ...]:
    """Nama tool yang dilihat klien MCP.

    Dibaca lewat ``list_tools()``, yaitu jalur yang sama dengan yang dipakai
    klien, bukan lewat internal FastMCP: yang perlu dijamin adalah apa yang
    sampai ke agent.
    """
    return tuple(tool.name for tool in await server.list_tools())


async def tool_schemas(server: FastMCP) -> dict[str, dict[str, object]]:
    """Skema input tiap tool, seperti yang dikirim ke klien."""
    return {tool.name: tool.inputSchema for tool in await server.list_tools()}


def serve(env_file: Path, *, repo_root: Path) -> int:
    """Jalankan Guard sebagai server MCP stdio. Satu proses, satu agent.

    Identitas agent datang dari mandate yang ditunjuk ``GUARD_MANDATE_PATH``,
    bukan dari argumen (PRD v2 bagian 6.1). Heartbeat berjalan di thread
    sendiri; begitu ia gagal, sesi ditandai tidak sehat, tool berhenti
    memutuskan, dan proses keluar dengan kode non-nol.

    Kegagalan konfigurasi dan kegagalan start dilaporkan ke stderr, bukan ke
    stdout: stdout adalah transport MCP, dan menulis apa pun di sana akan
    merusak protokolnya.
    """
    try:
        config = load_guard_config(env_file, repo_root=repo_root)
    except GuardConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        session = start_guard(config)
    except GuardStartupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    exit_code = 0

    def on_heartbeat_failure() -> None:
        nonlocal exit_code
        exit_code = 1

    heartbeat = Heartbeat(session, on_failure=on_heartbeat_failure)
    heartbeat.start()
    try:
        build_server(session).run()
    finally:
        heartbeat.stop()
        session.close()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    """Jalankan server langsung lewat ``python -m``, tanpa melalui ``guard``."""
    arguments = sys.argv[1:] if argv is None else argv
    repo_root = Path(__file__).resolve().parents[2]
    env_file = Path(arguments[0]) if arguments else repo_root / RUNTIME_ENV_FILENAME
    return serve(env_file, repo_root=repo_root)


def run_with_session(session: GuardSession, runner: Callable[[FastMCP], None]) -> None:
    """Jalankan ``runner`` dengan server yang dibangun dari ``session``.

    Hanya untuk tes dan penggunaan tertanam; ``main`` adalah jalur normal.
    """
    runner(build_server(session))
