"""Smoke test CLI: setiap modul bisa diimpor, setiap subcommand punya ``--help``.

Dua hal yang dijaga di sini:

* Modul yang tidak pernah diimpor oleh tes bisa punya impor yang salah dan
  tetap "hijau", karena tidak ada yang memuatnya. Itu sudah pernah terjadi di
  repo ini: ``ruff --fix`` membuang ``from pathlib import Path`` dari sebuah
  skrip, seluruh tes tetap lulus karena ``Path`` hanya dipakai di ``main``,
  dan yang menangkapnya adalah ruff — bukan tes. **Suite yang hijau bukan
  bukti entry point-nya jalan.**
* ``--help`` adalah jalur yang paling sering dipakai manusia dan paling
  jarang diuji. Argparse akan meledak saat merakit teks bantuan bila ada
  ``%`` yang tidak di-escape atau ``choices`` yang bukan iterable, dan itu
  hanya terlihat ketika ``--help`` benar-benar dijalankan.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
from pathlib import Path

import pytest

import payment_guard
from payment_guard.cli import build_parser, main

ROOT = Path(__file__).resolve().parents[2]

#: Pohon subcommand, ditulis sebagai argv. Diperiksa terhadap parser di bawah,
#: jadi daftar yang tertinggal dari kode akan gagal, bukan diam.
SUBCOMMANDS: tuple[tuple[str, ...], ...] = (
    (),
    ("db",),
    ("db", "migrate"),
    ("db", "status"),
    ("db", "bootstrap-roles"),
    ("keygen",),
    ("serve",),
    ("approvals",),
    ("approvals", "list"),
    ("approvals", "approve"),
    ("approvals", "reject"),
    ("execute",),
    ("recover",),
    ("watch",),
    ("reconcile",),
    ("wallet",),
    ("verify",),
)


def pilihan_subparser(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """Subcommand satu tingkat di bawah ``parser``, atau dict kosong.

    Argparse tidak punya API publik untuk ini, jadi atribut privatnya dibaca
    di **satu** tempat saja. Kalau argparse mengubah bentuknya, yang gagal
    satu fungsi, bukan tiga tes.
    """
    grup = getattr(parser, "_subparsers", None)
    if grup is None:
        return {}
    pilihan: dict[str, argparse.ArgumentParser] = {}
    for aksi in grup._group_actions:
        pilihan.update(getattr(aksi, "choices", {}) or {})
    return pilihan


def pohon_dari_parser() -> tuple[set[str], set[tuple[str, str]]]:
    """``({subcommand akar}, {(akar, anak)})`` dari ``build_parser``."""
    akar = pilihan_subparser(build_parser())
    bersarang = {(nama, anak) for nama, sub in akar.items() for anak in pilihan_subparser(sub)}
    return set(akar), bersarang


def modul_payment_guard() -> list[str]:
    """Nama setiap modul di ``src/payment_guard/``, dari filesystem."""
    paket = Path(payment_guard.__file__).parent
    return sorted(m.name for m in pkgutil.iter_modules([str(paket)]) if not m.name.startswith("_"))


class TestSetiapModulBisaDiimpor:
    def test_ada_modul_yang_ditemukan(self) -> None:
        """Penjaga untuk penjaganya: daftar kosong akan membuat tes di bawah lolos."""
        assert len(modul_payment_guard()) >= 20

    @pytest.mark.parametrize("nama", modul_payment_guard())
    def test_impor(self, nama: str) -> None:
        importlib.import_module(f"payment_guard.{nama}")


class TestSetiapSubcommandPunyaHelp:
    @pytest.mark.parametrize("argv", SUBCOMMANDS, ids=lambda a: " ".join(a) or "(akar)")
    def test_help_keluar_nol(
        self, argv: tuple[str, ...], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--help`` harus keluar dengan SystemExit(0) dan mencetak usage."""
        with pytest.raises(SystemExit) as info:
            main([*argv, "--help"])
        assert info.value.code == 0
        keluaran = capsys.readouterr().out
        assert keluaran.startswith("usage: guard")
        for bagian in argv:
            assert bagian in keluaran

    def test_daftar_subcommand_cocok_dengan_parser(self) -> None:
        """Daftar di atas tidak boleh tertinggal dari ``build_parser``.

        Subcommand baru di kode tanpa baris di :data:`SUBCOMMANDS` berarti
        ``--help``-nya tidak pernah diuji, dan itu persis celah yang tes ini
        ada untuk menutup.
        """
        akar, bersarang = pohon_dari_parser()
        didaftar_akar = {a[0] for a in SUBCOMMANDS if len(a) == 1}
        didaftar_bersarang = {(a[0], a[1]) for a in SUBCOMMANDS if len(a) == 2}
        assert didaftar_akar == akar, f"akar: kode {sorted(akar)} vs daftar {sorted(didaftar_akar)}"
        assert didaftar_bersarang == bersarang, (
            f"bersarang: kode {sorted(bersarang)} vs daftar {sorted(didaftar_bersarang)}"
        )


class TestHelpTidakMenawarkanJalanPintas:
    """ST-23: tidak boleh ada flag yang melewati konfirmasi manusia."""

    @pytest.mark.parametrize("argv", [("approvals", "approve"), ("approvals", "reject")])
    def test_tanpa_yes(self, argv: tuple[str, ...], capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            main([*argv, "--help"])
        keluaran = capsys.readouterr().out
        for terlarang in ("--yes", "--force", "--no-confirm", "--non-interactive"):
            assert terlarang not in keluaran
