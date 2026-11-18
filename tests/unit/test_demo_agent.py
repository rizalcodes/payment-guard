"""Agent demo: tepat tiga tool, tanpa agent_id, tanpa jalan keluar.

PRD v2.1 bagian 4, Security v1.1 S1, E2, ST-01, ST-22.

Yang dijaga di sini adalah **permukaan** agent. Bukan "apakah agent berperilaku
baik" — itu urusan eval di M8 — melainkan apakah daftar kemampuannya memang
tertutup. Agent yang bisa menjalankan perintah atau membaca berkas sembarang
tidak lagi dibatasi oleh tool mana pun, jadi daftar itu harus bisa diperiksa.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from payment_guard.chains import (
    BASE_SEPOLIA,
    DEVNET_LOCAL,
    NETWORK_DEVNET,
    NETWORK_PRODUCTION,
)
from payment_guard.demo_agent import (
    GUARD_TOOL_NAMES,
    LOCAL_TOOL_NAMES,
    TOOL_ARGUMENTS,
    TOOL_NAMES,
    execute_payment,
    main,
    tool_names,
)
from payment_guard.guard_service import load_signing_identity
from payment_guard.keygen import run_keygen
from payment_guard.signing import build_payload, sign

MODULE = Path(__file__).resolve().parents[2] / "src" / "payment_guard" / "demo_agent.py"

RECIPIENT = "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"
MANDATE_HASH = "a" * 64


@pytest.fixture
def keys(tmp_path: Path) -> dict[str, Path]:
    """Pasangan kunci uji di ``tmp_path``, di luar repo."""
    signing_path = tmp_path / "di-luar-repo" / "guard_signing.json"
    public_path = tmp_path / "guard_public.json"
    run_keygen(signing_path, public_path)
    return {"signing": signing_path, "public": public_path}


def envelope_for(
    keys: dict[str, Path],
    *,
    mandate_hash: str = MANDATE_HASH,
    amount_base: int = 10_000_000,
    chain: str = BASE_SEPOLIA,
    network: str = NETWORK_PRODUCTION,
) -> dict:
    """Envelope sah yang **benar-benar ditandatangani** kunci uji.

    ``chain`` dan ``network`` bawaannya produksi, jadi pemanggil lama tidak
    berubah. Keduanya ada supaya envelope devnet — yang ``chain_id``-nya
    31337, bukan 84532 — bisa dibuat tanpa memalsukan tanda tangan.
    """
    identity = load_signing_identity(keys["signing"])
    payload = build_payload(
        decision_id="123e4567-e89b-42d3-a456-426614174000",
        mandate_id="m-001",
        agent_id="demo-agent",
        recipient=RECIPIENT,
        amount_base=amount_base,
        issued_at=1_800_000_000,
        valid_until=1_800_000_900,
        mandate_hash=mandate_hash,
        chain=chain,
        network=network,
    )
    return sign(payload, identity.signer, identity.key_id).as_dict()


class TestExactlyThreeTools:
    def test_three_names(self) -> None:
        assert len(TOOL_NAMES) == 3
        assert tool_names() == TOOL_NAMES

    def test_the_documented_three(self) -> None:
        assert set(TOOL_NAMES) == {"check_payment", "get_decision_status", "execute_payment"}

    def test_two_come_from_guard_and_one_is_local(self) -> None:
        """``execute_payment`` bukan tool Guard (PRD v2.1 nomor 2)."""
        assert set(GUARD_TOOL_NAMES) == {"check_payment", "get_decision_status"}
        assert set(LOCAL_TOOL_NAMES) == {"execute_payment"}
        assert set(GUARD_TOOL_NAMES) | set(LOCAL_TOOL_NAMES) == set(TOOL_NAMES)
        assert not set(GUARD_TOOL_NAMES) & set(LOCAL_TOOL_NAMES)

    def test_every_tool_has_declared_arguments(self) -> None:
        assert set(TOOL_ARGUMENTS) == set(TOOL_NAMES)

    @pytest.mark.parametrize(
        "terlarang",
        [
            "shell",
            "bash",
            "spawn",
            "exec_",
            "run_command",
            "read_file",
            "write_file",
            "open_file",
            "browse",
            "fetch",
            "http_get",
            "search",
            "python",
        ],
    )
    def test_no_shell_file_or_browsing_tool(self, terlarang: str) -> None:
        """Penjaga untuk tool yang mungkin ditambahkan kelak.

        ``exec_`` memakai garis bawah dengan sengaja: ``execute_payment``
        memuat "exec" dan ia justru salah satu dari tiga tool yang sah.
        """
        for name in TOOL_NAMES:
            assert terlarang not in name, name


class TestNoAgentIdInArguments:
    """ST-01: identitas datang dari mandate, bukan dari argumen."""

    def test_no_tool_takes_agent_id(self) -> None:
        for name, arguments in TOOL_ARGUMENTS.items():
            assert "agent_id" not in arguments, name

    def test_no_tool_takes_a_mandate_or_a_key(self) -> None:
        for name, arguments in TOOL_ARGUMENTS.items():
            for terlarang in ("mandate_path", "key_id", "signing_key", "private_key"):
                assert terlarang not in arguments, (name, terlarang)

    def test_check_payment_arguments_match_the_tool_contract(self) -> None:
        """PRD v2 bagian 7: empat argumen, dan itu saja."""
        assert TOOL_ARGUMENTS["check_payment"] == ("recipient", "amount", "chain", "reason")

    def test_get_decision_status_takes_only_the_id(self) -> None:
        assert TOOL_ARGUMENTS["get_decision_status"] == ("decision_id",)


class TestExecutePaymentStub:
    """M4: verifikasi envelope dengan public key saja. Tanpa chain, tanpa wallet."""

    def test_valid_envelope_verifies(self, keys: dict[str, Path]) -> None:
        hasil = execute_payment(envelope_for(keys), MANDATE_HASH, public_keys_path=keys["public"])
        assert hasil.verified is True
        assert hasil.recipient == RECIPIENT
        assert hasil.amount_base == "10000000"

    def test_nothing_is_broadcast(self, keys: dict[str, Path]) -> None:
        """M4 berhenti setelah verifikasi; broadcast adalah M5."""
        hasil = execute_payment(envelope_for(keys), MANDATE_HASH, public_keys_path=keys["public"])
        assert hasil.broadcast is False

    def test_missing_envelope_is_refused(self, keys: dict[str, Path]) -> None:
        hasil = execute_payment(None, MANDATE_HASH, public_keys_path=keys["public"])
        assert hasil.verified is False
        assert "tidak ada signed_decision" in hasil.detail

    def test_tampered_amount_is_refused(self, keys: dict[str, Path]) -> None:
        envelope = envelope_for(keys)
        envelope["payload"]["amount_base"] = "99000000"
        hasil = execute_payment(envelope, MANDATE_HASH, public_keys_path=keys["public"])
        assert hasil.verified is False
        assert "ditolak" in hasil.detail

    def test_tampered_recipient_is_refused(self, keys: dict[str, Path]) -> None:
        envelope = envelope_for(keys)
        envelope["payload"]["recipient"] = "0x" + "9" * 40
        hasil = execute_payment(envelope, MANDATE_HASH, public_keys_path=keys["public"])
        assert hasil.verified is False

    def test_a_valid_envelope_for_the_wrong_mandate_is_refused(self, keys: dict[str, Path]) -> None:
        """Tanda tangan sah atas aturan yang salah tetap salah (Arsitektur 7, cek 4b)."""
        hasil = execute_payment(
            envelope_for(keys, mandate_hash="b" * 64),
            MANDATE_HASH,
            public_keys_path=keys["public"],
        )
        assert hasil.verified is False
        assert "mandate_hash" in hasil.detail

    def test_envelope_signed_by_another_key_is_refused(self, tmp_path: Path) -> None:
        kunci_a = {"signing": tmp_path / "a" / "s.json", "public": tmp_path / "a_public.json"}
        run_keygen(kunci_a["signing"], kunci_a["public"])
        kunci_b = {"signing": tmp_path / "b" / "s.json", "public": tmp_path / "b_public.json"}
        run_keygen(kunci_b["signing"], kunci_b["public"])

        hasil = execute_payment(
            envelope_for(kunci_a), MANDATE_HASH, public_keys_path=kunci_b["public"]
        )
        assert hasil.verified is False

    def test_garbage_envelope_is_an_answer_not_a_crash(self, keys: dict[str, Path]) -> None:
        for sampah in ({"bukan": "envelope"}, {"payload": {}, "key_id": "x", "signature": "y"}):
            hasil = execute_payment(sampah, MANDATE_HASH, public_keys_path=keys["public"])
            assert hasil.verified is False

    def test_refusal_never_names_key_material(self, keys: dict[str, Path]) -> None:
        envelope = envelope_for(keys)
        envelope["payload"]["amount_base"] = "1"
        hasil = execute_payment(envelope, MANDATE_HASH, public_keys_path=keys["public"])
        rahasia = json.loads(keys["signing"].read_text(encoding="utf-8"))["private_key"]
        assert rahasia not in hasil.detail


class TestCommandLine:
    """Agent demo harus benar-benar bisa dijalankan.

    Tes di kelas ini ada karena M4 mengirim ``main`` yang tidak bisa dipakai:
    ia mengubah ``argv`` kosong menjadi ``[]`` alih-alih meneruskan ``None``,
    sehingga setiap argumen dari baris perintah diabaikan, dan modulnya tidak
    punya blok ``__main__`` sehingga ``python -m`` tidak menjalankan apa pun.
    Keduanya lolos karena tidak ada satu pun tes yang memanggil ``main``.
    """

    def test_help_exits_cleanly(self) -> None:
        with pytest.raises(SystemExit) as info:
            main(["--help"])
        assert info.value.code == 0

    def test_the_two_required_arguments_are_enforced(self) -> None:
        with pytest.raises(SystemExit) as info:
            main([])
        assert info.value.code == 2

    def test_recipient_and_amount_are_positional(self, monkeypatch) -> None:
        """Panduan demo memanggilnya tanpa flag; bentuknya harus cocok."""
        dicatat: dict[str, object] = {}

        async def tiruan(recipient, amount, reason, **kwargs):
            dicatat.update({"recipient": recipient, "amount": amount, "reason": reason})
            dicatat.update(kwargs)
            return {"ok": True}

        monkeypatch.setattr("payment_guard.demo_agent.run_once", tiruan)
        assert main([RECIPIENT, "1.5", "--reason", "demo"]) == 0
        assert dicatat["recipient"] == RECIPIENT
        assert dicatat["amount"] == "1.5"
        assert dicatat["reason"] == "demo"

    def test_the_amount_stays_a_string(self, monkeypatch) -> None:
        """Uang tidak pernah lewat ``float``, termasuk saat di-parse argparse."""
        dicatat: dict[str, object] = {}

        async def tiruan(recipient, amount, reason, **kwargs):
            dicatat["amount"] = amount
            return {}

        monkeypatch.setattr("payment_guard.demo_agent.run_once", tiruan)
        main([RECIPIENT, "0.000001"])
        assert dicatat["amount"] == "0.000001"
        assert isinstance(dicatat["amount"], str)

    def test_the_module_can_be_run_with_dash_m(self) -> None:
        """``python -m payment_guard.demo_agent`` butuh blok ``__main__``."""
        source = MODULE.read_text(encoding="utf-8")
        assert '__name__ == "__main__"' in source
        assert "raise SystemExit(main())" in source


class TestNoEscapeHatches:
    """Permukaan modul, diperiksa di AST, bukan dengan menjalankannya."""

    def source(self) -> ast.Module:
        return ast.parse(MODULE.read_text(encoding="utf-8"))

    @pytest.mark.parametrize(
        "modul",
        ["subprocess", "os", "shutil", "socket", "requests", "httpx", "urllib", "webbrowser"],
    )
    def test_module_is_not_imported(self, modul: str) -> None:
        """Agent tidak punya jalur sendiri ke proses, berkas, atau jaringan.

        Transport stdio MCP memang menjalankan ``guard serve`` sebagai
        subproses, tapi itu dikerjakan SDK di dalam ``stdio_client``, bukan
        kemampuan yang ditawarkan ke agent.
        """
        diimpor: set[str] = set()
        for node in ast.walk(self.source()):
            if isinstance(node, ast.Import):
                diimpor.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                diimpor.add(node.module.split(".")[0])
        assert modul not in diimpor

    def test_no_eval_or_exec(self) -> None:
        panggilan = [
            node.func.id
            for node in ast.walk(self.source())
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        for terlarang in ("eval", "exec", "compile", "__import__"):
            assert terlarang not in panggilan

    def test_no_llm_in_m4(self) -> None:
        """Model menyusul di M8; M4 harus deterministik supaya bisa diuji."""
        teks = MODULE.read_text(encoding="utf-8").lower()
        for penyedia in ("openai", "anthropic", "litellm", "langchain"):
            assert f"import {penyedia}" not in teks
            assert f"from {penyedia}" not in teks

    def test_no_wallet_key_is_read(self) -> None:
        teks = MODULE.read_text(encoding="utf-8")
        assert "EXECUTOR_KEY_PATH" not in teks
        assert "load_signing_identity" not in teks


class TestNetworkDiteruskanKeVerifikasi:
    """Bug 17 Nov 2026: ``execute_payment`` memanggil ``verify`` tanpa jaringan.

    Sebelum perbaikan, baris itu berbunyi ``verify(envelope, registry)``, jadi
    ``verify`` memakai bawaannya sendiri — ``base-sepolia`` dan chain id 84532.
    Envelope devnet ber-``chain_id`` 31337, jadi agent demo **selalu**
    menolaknya, dan tidak ada flag untuk memberi tahu agent bahwa ia sedang di
    devnet. Jalur MCP di devnet karena itu tidak pernah bisa selesai.

    Envelope di kelas ini ditandatangani sungguhan dengan kunci uji; tidak ada
    tanda tangan tiruan. Yang membedakan kedua kasus hanya argumen ``network``.
    """

    def test_envelope_devnet_diterima_saat_network_devnet(self, keys: dict[str, Path]) -> None:
        envelope = envelope_for(keys, chain=DEVNET_LOCAL, network=NETWORK_DEVNET)
        assert envelope["payload"]["chain_id"] == 31337

        hasil = execute_payment(
            envelope, MANDATE_HASH, public_keys_path=keys["public"], network=NETWORK_DEVNET
        )
        assert hasil.verified, hasil.detail

    def test_envelope_devnet_ditolak_saat_bawaan(self, keys: dict[str, Path]) -> None:
        """Penjaga bawaan: kalau ini lulus, bawaan produksi diam-diam berubah."""
        envelope = envelope_for(keys, chain=DEVNET_LOCAL, network=NETWORK_DEVNET)

        hasil = execute_payment(envelope, MANDATE_HASH, public_keys_path=keys["public"])
        assert not hasil.verified
        assert "31337" in hasil.detail
        assert "84532" in hasil.detail

    def test_envelope_produksi_ditolak_saat_network_devnet(self, keys: dict[str, Path]) -> None:
        """Dua arah: jaringan bukan saklar yang melonggarkan, melainkan pilihan."""
        envelope = envelope_for(keys)
        assert envelope["payload"]["chain_id"] == 84532

        hasil = execute_payment(
            envelope, MANDATE_HASH, public_keys_path=keys["public"], network=NETWORK_DEVNET
        )
        assert not hasil.verified
        assert "84532" in hasil.detail

    @pytest.mark.parametrize(
        ("network", "diterima"),
        [(NETWORK_PRODUCTION, False), (NETWORK_DEVNET, True)],
    )
    def test_hasil_mengikuti_argumen_bukan_konstanta(
        self, keys: dict[str, Path], network: str, diterima: bool
    ) -> None:
        """Satu envelope, dua jaringan, dua hasil.

        Kalau jaringan tertanam sebagai konstanta di ``execute_payment``, kedua
        baris parametrize ini akan memberi hasil yang sama dan salah satunya
        gagal. Itulah yang membuktikan nilainya dibaca dari argumen.
        """
        envelope = envelope_for(keys, chain=DEVNET_LOCAL, network=NETWORK_DEVNET)
        hasil = execute_payment(
            envelope, MANDATE_HASH, public_keys_path=keys["public"], network=network
        )
        assert hasil.verified is diterima

    def test_bawaan_execute_payment_tetap_produksi(self) -> None:
        """Bawaan tertulis, bukan tersirat; perubahannya harus terlihat di diff."""
        import inspect

        param = inspect.signature(execute_payment).parameters["network"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default == NETWORK_PRODUCTION

    def test_bawaan_run_once_tetap_produksi(self) -> None:
        import inspect

        from payment_guard.demo_agent import run_once

        param = inspect.signature(run_once).parameters["network"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default == NETWORK_PRODUCTION


class TestFlagNetworkDiBarisPerintah:
    def test_network_tidak_dikenal_ditolak_argparse(self) -> None:
        with pytest.raises(SystemExit) as info:
            main([RECIPIENT, "1", "--network", "devnett"])
        assert info.value.code == 2

    def test_network_diteruskan_ke_run_once(self, monkeypatch, tmp_path: Path) -> None:
        """Flag yang tidak sampai ke pemanggilnya sama saja dengan tidak ada.

        ``--public-keys-file`` ikut diberikan karena di devnet ia wajib; lihat
        :class:`TestBawaanMengikutiNetwork`. Versi pertama tes ini tidak
        memberikannya, dan itu lolos hanya selama devnet masih diam-diam
        memakai registri produksi.
        """
        dicatat: dict[str, object] = {}

        async def tiruan(recipient, amount, reason, **kwargs):
            dicatat.update(kwargs)
            return {}

        pub = tmp_path / "pub.json"
        pub.write_text("{}", encoding="utf-8")

        monkeypatch.setattr("payment_guard.demo_agent.run_once", tiruan)
        main([RECIPIENT, "1", "--network", NETWORK_DEVNET, "--public-keys-file", str(pub)])
        assert dicatat["network"] == NETWORK_DEVNET

    def test_tanpa_flag_network_tetap_produksi(self, monkeypatch) -> None:
        dicatat: dict[str, object] = {}

        async def tiruan(recipient, amount, reason, **kwargs):
            dicatat.update(kwargs)
            return {}

        monkeypatch.setattr("payment_guard.demo_agent.run_once", tiruan)
        main([RECIPIENT, "1"])
        assert dicatat["network"] == NETWORK_PRODUCTION

    def test_pilihan_network_diambil_dari_chains(self) -> None:
        """Daftar pilihan tidak ditulis ulang di demo_agent."""
        from payment_guard.chains import supported_networks

        with pytest.raises(SystemExit):
            main([RECIPIENT, "1", "--help"])
        assert set(supported_networks()) == {NETWORK_PRODUCTION, NETWORK_DEVNET}


class TestBawaanMengikutiNetwork:
    """Bawaan ``--public-keys-file`` dan ``--chain`` harus ikut ``--network``.

    Dua bawaan lama keduanya produksi dan keduanya salah untuk devnet:

    * ``--public-keys-file`` bawaannya ``guard_public.json`` di akar repo —
      registri **produksi**. Dipakai di devnet, ia tidak gagal karena berkasnya
      hilang (berkas itu ada), melainkan karena ``key_id``-nya tidak dikenal.
      Kegagalan yang menyesatkan lebih buruk daripada yang jelas, jadi di
      devnet tidak ada bawaan sama sekali: flag itu wajib.
    * ``--chain`` bawaannya ``base-sepolia``, yang **tidak sah** di jaringan
      devnet; ``check_payment`` menolaknya dua kali, di ``chain_id_for`` dan
      di pembandingan dengan chain mandate.

    Argumen yang diberikan eksplisit tidak pernah ditulis ulang, termasuk
    kombinasi yang akan ditolak Guard. Menebak maksud operator di sini berarti
    menyembunyikan kesalahannya.
    """

    @pytest.fixture
    def dicatat(self, monkeypatch) -> dict[str, object]:
        hasil: dict[str, object] = {}

        async def tiruan(recipient, amount, reason, **kwargs):
            hasil.update(kwargs)
            return {}

        monkeypatch.setattr("payment_guard.demo_agent.run_once", tiruan)
        return hasil

    def test_devnet_tanpa_flag_kunci_gagal_dengan_pesan_jelas(
        self, dicatat: dict[str, object], capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as info:
            main([RECIPIENT, "10", "--network", NETWORK_DEVNET])
        assert info.value.code != 0

        pesan = capsys.readouterr().err
        assert "--public-keys-file" in pesan
        assert NETWORK_DEVNET in pesan
        assert dicatat == {}, "run_once tidak boleh dipanggil kalau argumennya ditolak"

        # Pesannya tidak boleh menyiratkan bahwa agent membaca berkas env:
        # ia tidak membacanya sama sekali. `--env-file` hanya diteruskan
        # sebagai argumen ke subproses `guard serve`, dan servernyalah yang
        # membacanya. Versi pertama pesan ini berbunyi "Tunjuk
        # GUARD_PUBLIC_KEYS_PATH dari berkas env devnet Anda", yang bisa
        # dibaca sebagai janji bahwa nilainya diambil otomatis.
        assert "tidak membaca berkas env" in pesan

    def test_agent_tidak_membaca_env_sama_sekali(self) -> None:
        """Dasar klaim di pesan error itu, diperiksa di sumbernya."""
        sumber = MODULE.read_text(encoding="utf-8")
        for terlarang in ("read_env_file", "os.environ", "getenv", "dotenv"):
            assert terlarang not in sumber, terlarang

    def test_devnet_memakai_chain_devnet_local(
        self, dicatat: dict[str, object], tmp_path: Path
    ) -> None:
        pub = tmp_path / "guard_public_devnet.json"
        pub.write_text("{}", encoding="utf-8")

        assert (
            main([RECIPIENT, "10", "--network", NETWORK_DEVNET, "--public-keys-file", str(pub)])
            == 0
        )
        assert dicatat["chain"] == DEVNET_LOCAL
        assert dicatat["network"] == NETWORK_DEVNET
        assert dicatat["public_keys_path"] == pub

    def test_produksi_tanpa_flag_tetap_seperti_dulu(self, dicatat: dict[str, object]) -> None:
        """Penjaga regresi: perilaku produksi tidak boleh bergeser sedikit pun."""
        assert main([RECIPIENT, "10"]) == 0
        assert dicatat["chain"] == BASE_SEPOLIA
        assert dicatat["network"] == NETWORK_PRODUCTION
        assert dicatat["public_keys_path"] == Path("guard_public.json")

    def test_chain_eksplisit_tidak_ditulis_ulang(
        self, dicatat: dict[str, object], tmp_path: Path
    ) -> None:
        """Kombinasi yang akan ditolak Guard tetap diteruskan apa adanya."""
        pub = tmp_path / "pub.json"
        pub.write_text("{}", encoding="utf-8")

        assert (
            main(
                [
                    RECIPIENT,
                    "10",
                    "--network",
                    NETWORK_DEVNET,
                    "--chain",
                    BASE_SEPOLIA,
                    "--public-keys-file",
                    str(pub),
                ]
            )
            == 0
        )
        assert dicatat["chain"] == BASE_SEPOLIA

    def test_kunci_eksplisit_dipakai_di_produksi(
        self, dicatat: dict[str, object], tmp_path: Path
    ) -> None:
        pub = tmp_path / "lain.json"
        pub.write_text("{}", encoding="utf-8")

        assert main([RECIPIENT, "10", "--public-keys-file", str(pub)]) == 0
        assert dicatat["public_keys_path"] == pub
