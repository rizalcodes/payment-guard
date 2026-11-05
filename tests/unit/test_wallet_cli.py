"""``guard wallet``: cetak alamat wallet Executor, dan tidak lebih.

Perintah ini ada karena satu kekeliruan di M6.3: checklist operator menyuruh
menjalankan ``guard execute`` untuk **membaca** alamat wallet dari laporan
startup-nya, dan ``guard execute`` menyiarkan transaksi bila seluruh ceknya
lolos. Tidak ada mode baca-saja di perintah itu.

Yang dijaga berkas ini karena itu bukan hanya "alamatnya benar", tapi juga
"perintahnya tidak bisa melakukan apa pun selain itu": tanpa database, tanpa
jaringan, tanpa argumen yang bisa mengubah perilakunya.

Tidak ada database dan tidak ada jaringan di berkas ini, dan dua tes di bawah
membuktikannya dengan memasang pengganti yang **gagal bila dipanggil**.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest

from fake_chain import TEST_WALLET_ADDRESS, TEST_WALLET_SEED, write_test_wallet
from payment_guard.cli import build_parser, main
from payment_guard.config import EXECUTOR_ENV_VARS, RUNTIME_ENV_FILENAME

CLI_MODULE = Path(__file__).resolve().parents[2] / "src" / "payment_guard" / "cli.py"

#: Bentuk baris keluaran yang dijanjikan: ``wallet`` lalu alamat lowercase.
WALLET_LINE = re.compile(r"\Awallet 0x[0-9a-f]{40}\Z")

KEY_VAR = EXECUTOR_ENV_VARS["wallet_key_path"]


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Berkas ``.env`` sementara, dan environment yang bersih.

    Variabel nyata dibersihkan supaya ``.env`` operator maupun environment
    shell tidak bisa menentukan hasil tes.
    """
    monkeypatch.delenv(KEY_VAR, raising=False)
    path = tmp_path / RUNTIME_ENV_FILENAME
    path.write_text("", encoding="utf-8")
    return path


def write_env(env_file: Path, key_path: Path) -> None:
    env_file.write_text(f"{KEY_VAR}={key_path}\n", encoding="utf-8")


def run_wallet(env_file: Path) -> int:
    return main(["wallet", "--env-file", str(env_file)])


# --------------------------------------------------------------------------- #
# Keluaran
# --------------------------------------------------------------------------- #


class TestOutput:
    def test_it_prints_exactly_one_line_in_the_promised_shape(
        self, env_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write_env(env_file, write_test_wallet(tmp_path / "di-luar-repo"))

        assert run_wallet(env_file) == 0

        captured = capsys.readouterr()
        lines = captured.out.splitlines()
        assert len(lines) == 1, captured.out
        assert WALLET_LINE.match(lines[0]), lines[0]
        assert captured.err == ""

    def test_the_address_is_the_one_derived_from_the_key(
        self, env_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write_env(env_file, write_test_wallet(tmp_path / "di-luar-repo"))
        run_wallet(env_file)
        assert capsys.readouterr().out.strip() == f"wallet {TEST_WALLET_ADDRESS}"

    def test_it_matches_what_the_executor_startup_report_prints(
        self, env_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Dua perintah, satu angka. Itu yang membuat checklist bisa dibandingkan.

        Laporan startup Executor mencetak ``wallet <alamat>`` dari
        ``runtime.wallet.address``; ``guard wallet`` mencetaknya dari
        ``load_wallet`` atas berkas yang sama. Kalau keduanya pernah
        menyimpang, langkah (a) dan (b) di checklist akan membandingkan dua
        hal yang berbeda dan tetap terlihat cocok.
        """
        from payment_guard.chain import check_startup, load_wallet

        key_path = write_test_wallet(tmp_path / "di-luar-repo")
        write_env(env_file, key_path)
        run_wallet(env_file)
        dari_cli = capsys.readouterr().out.strip()

        # Sisi Executor: `check_startup` yang menghasilkan `report.wallet_address`
        # yang dicetak `start_executor`. Klien tiruan dipakai supaya tidak ada
        # jaringan; yang dibandingkan hanya alamatnya.
        from fake_chain import FakeChain

        report = check_startup(
            FakeChain(),
            expected_chain_id=84532,
            usdc_address="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            wallet_address=load_wallet(key_path).address,
            minimum_balance_wei=0,
        )
        assert dari_cli == f"wallet {report.wallet_address}"

    def test_no_other_field_of_the_key_file_is_printed(
        self, env_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Berkas kunci punya dua field; hanya satu yang boleh keluar."""
        key_path = write_test_wallet(tmp_path / "di-luar-repo")
        write_env(env_file, key_path)
        run_wallet(env_file)

        captured = capsys.readouterr()
        keluaran = captured.out + captured.err
        assert TEST_WALLET_SEED not in keluaran
        assert "private_key" not in keluaran
        assert re.search(r"0x[0-9a-fA-F]{64}", keluaran) is None, keluaran


# --------------------------------------------------------------------------- #
# Kegagalan
# --------------------------------------------------------------------------- #


class TestFailures:
    def test_a_missing_key_file_exits_one(
        self, env_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write_env(env_file, tmp_path / "tidak-ada" / "wallet.json")

        assert run_wallet(env_file) == 1

        captured = capsys.readouterr()
        assert captured.out == "", "tidak boleh mencetak baris wallet saat gagal"
        assert "error:" in captured.err

    def test_an_unset_variable_exits_one_and_names_it(
        self, env_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Tanpa default. Variabel kosong adalah kegagalan, bukan asumsi."""
        assert run_wallet(env_file) == 1
        assert KEY_VAR in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("name", "content"),
        [
            ("bukan-json", "{ini bukan json"),
            ("tanpa-field", json.dumps({"address": TEST_WALLET_ADDRESS})),
            ("kunci-pendek", json.dumps({"address": TEST_WALLET_ADDRESS, "private_key": "0x01"})),
            (
                "alamat-tidak-cocok",
                json.dumps(
                    {
                        "address": "0x1111111111111111111111111111111111111111",
                        "private_key": TEST_WALLET_SEED,
                    }
                ),
            ),
            ("field-berlebih", json.dumps({"address": "a", "private_key": "b", "extra": 1})),
        ],
    )
    def test_a_corrupt_key_file_exits_one_without_leaking_it(
        self,
        env_file: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        name: str,
        content: str,
    ) -> None:
        """Pesannya menyebut berkasnya dan alasannya, bukan isinya.

        Jalur gagal adalah jalur yang pesannya dibaca manusia, dan satu-satunya
        saat isi berkas kunci paling mungkin bocor ke layar.
        """
        key_path = tmp_path / "di-luar-repo" / f"{name}.json"
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text(content, encoding="utf-8")
        write_env(env_file, key_path)

        assert run_wallet(env_file) == 1

        captured = capsys.readouterr()
        assert captured.out == ""
        assert TEST_WALLET_SEED not in captured.err
        assert re.search(r"0x[0-9a-fA-F]{64}", captured.err) is None, captured.err

    def test_a_key_file_inside_the_repo_is_refused(
        self, env_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Pemeriksaan yang sama dengan yang dilakukan pemuat Executor."""
        write_env(env_file, CLI_MODULE)
        assert run_wallet(env_file) == 1
        assert "error:" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Tidak menyentuh apa pun
# --------------------------------------------------------------------------- #


class TestItTouchesNothing:
    """Dibuktikan dengan pengganti yang **gagal bila dipanggil**.

    Pengganti yang hanya mencatat pemanggilan akan membuat tes lulus selama
    tidak ada yang memeriksa catatannya. Pengganti yang melempar tidak bisa
    diabaikan.
    """

    def test_it_opens_no_database_connection(
        self, env_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import psycopg

        def tolak(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("guard wallet membuka koneksi database")

        monkeypatch.setattr(psycopg, "connect", tolak)
        write_env(env_file, write_test_wallet(tmp_path / "di-luar-repo"))

        assert run_wallet(env_file) == 0

    def test_it_opens_no_rpc_connection(
        self, env_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import payment_guard.chain as chain_module

        def tolak(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("guard wallet menyentuh RPC")

        monkeypatch.setattr(chain_module, "Web3", tolak)
        monkeypatch.setattr(chain_module, "Web3Client", tolak)
        write_env(env_file, write_test_wallet(tmp_path / "di-luar-repo"))

        assert run_wallet(env_file) == 0

    def test_it_never_reads_the_migrator_env_file(self, env_file: Path, tmp_path: Path) -> None:
        """Perintah runtime tidak pernah punya jalur ke URL migrator."""
        args = build_parser().parse_args(["wallet"])
        assert not hasattr(args, "migrator_env_file")

    def test_it_reads_only_the_key_path_variable(
        self, env_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``.env`` yang hanya memuat satu variabel itu sudah cukup.

        Kalau ia memakai ``load_executor_config``, berkas ini akan gagal
        karena ``EXECUTOR_DB_URL`` dan ``RPC_URL`` tidak ada — dan perintah
        baca yang gagal karena hal yang tidak dipakainya akan mendorong orang
        kembali memakai ``guard execute``.
        """
        write_env(env_file, write_test_wallet(tmp_path / "di-luar-repo"))
        isi = env_file.read_text(encoding="utf-8")
        assert "EXECUTOR_DB_URL" not in isi
        assert "RPC_URL" not in isi

        assert run_wallet(env_file) == 0
        assert capsys.readouterr().out.strip() == f"wallet {TEST_WALLET_ADDRESS}"


# --------------------------------------------------------------------------- #
# Permukaan perintah
# --------------------------------------------------------------------------- #


class TestTheCommandSurface:
    def test_the_subcommand_exists(self) -> None:
        assert build_parser().parse_args(["wallet"]).handler.__name__ == "_cmd_wallet"

    def test_it_takes_no_positional_argument(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["wallet", "sesuatu"])

    def test_its_only_option_is_env_file(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Perintah yang hanya bisa melakukan satu hal tidak bisa disuruh lain.

        Itu seluruh keunggulannya dibanding membaca alamat dari laporan
        startup ``guard execute``: tidak ada jalan dari sini ke penyiaran
        transaksi, bahkan dengan argumen yang salah ketik.

        Dibaca dari teks ``--help``, bukan dari atribut privat ``argparse``:
        yang terakhir berubah antar versi Python dan akan membuat tes ini
        gagal karena hal yang tidak ada hubungannya dengan perintahnya.
        """
        with pytest.raises(SystemExit):
            build_parser().parse_args(["wallet", "--help"])
        bantuan = capsys.readouterr().out
        assert set(re.findall(r"--[a-z-]+", bantuan)) == {"--env-file", "--help"}, bantuan

    @pytest.mark.parametrize("flag", ["--yes", "--broadcast", "--execute", "--force", "--send"])
    def test_no_flag_can_turn_it_into_a_write(self, flag: str) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["wallet", flag])

    def test_the_chain_module_is_imported_lazily(self) -> None:
        """``guard verify`` tidak boleh ikut memuat tumpukan chain.

        Aturan yang sama dengan ``serve``, ``execute``, dan ``watch``: impor
        berat hidup di dalam handler, bukan di tingkat modul ``cli``.
        """
        tree = ast.parse(CLI_MODULE.read_text(encoding="utf-8"))
        top_level = {
            node.module
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert "payment_guard.chain" not in top_level
