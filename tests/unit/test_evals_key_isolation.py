"""``EVAL_API_KEY`` hanya dibaca eval runner, dan tidak oleh komponen lain.

Arsitektur v1.1 bagian 2.1 mencatat kunci ini sebagai rahasia milik eval
runner. Guard, Executor, Watcher, dan Reconciler tidak boleh punya jalan ke
sana — bukan karena mereka akan menyalahgunakannya, tapi karena rahasia yang
bisa dibaca empat proses adalah rahasia yang bocornya tidak bisa dilacak.

Diperiksa dari sumber, bukan dari perilaku: perilaku membuktikan satu jalur,
sumber membuktikan tidak ada jalur.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from evals.adapters import EVAL_API_KEY_VAR

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "payment_guard"
EVALS = REPO / "evals"


def sumber_produk() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


class TestProductCodeCannotReadTheKey:
    def test_name_appears_nowhere_in_src(self) -> None:
        """Nama variabelnya tidak muncul di ``src/payment_guard/`` mana pun."""
        pelanggar = [
            berkas.relative_to(REPO).as_posix()
            for berkas in sumber_produk()
            if EVAL_API_KEY_VAR in berkas.read_text(encoding="utf-8")
        ]
        assert pelanggar == []

    def test_no_product_module_imports_evals(self) -> None:
        """Arah ketergantungan satu arah: ``evals`` memakai produk, bukan sebaliknya.

        Kalau sebuah modul produk mengimpor ``evals``, kunci itu berada satu
        impor saja dari proses yang berjalan di mesin operator.
        """
        pelanggar: list[str] = []
        for berkas in sumber_produk():
            modul = ast.parse(berkas.read_text(encoding="utf-8"))
            for node in ast.walk(modul):
                if isinstance(node, ast.Import):
                    if any(a.name.split(".")[0] == "evals" for a in node.names):
                        pelanggar.append(berkas.name)
                elif isinstance(node, ast.ImportFrom):
                    if (node.module or "").split(".")[0] == "evals":
                        pelanggar.append(berkas.name)
        assert pelanggar == []

    def test_anthropic_is_not_imported_by_product_code(self) -> None:
        """Produk tidak memanggil model mana pun. Itu pekerjaan eval."""
        pelanggar = [
            berkas.name
            for berkas in sumber_produk()
            if "import anthropic" in berkas.read_text(encoding="utf-8")
        ]
        assert pelanggar == []


class TestOnlyEvalsNamesTheKey:
    def test_the_name_is_defined_in_exactly_one_place(self) -> None:
        """Satu definisi, di ``evals/adapters.py``.

        Nama yang ditulis dua kali akan menyimpang: yang satu diubah, yang lain
        tidak, dan kunci dibaca dari variabel yang tidak diisi siapa pun.
        """
        definisi = [
            berkas.relative_to(REPO).as_posix()
            for berkas in sorted(EVALS.rglob("*.py"))
            if f'{EVAL_API_KEY_VAR}_VAR: Final = "{EVAL_API_KEY_VAR}"'
            in berkas.read_text(encoding="utf-8")
        ]
        assert definisi == ["evals/adapters.py"]

    def test_other_eval_modules_import_it(self) -> None:
        """Modul eval lain mengimpor nama itu, tidak menuliskannya ulang."""
        for nama in ("run.py",):
            teks = (EVALS / nama).read_text(encoding="utf-8")
            if EVAL_API_KEY_VAR in teks:
                assert "EVAL_API_KEY_VAR" in teks, nama


class TestEnvironmentDoesNotLeakTheKeyToGuard:
    def test_eval_env_file_never_carries_the_key(self) -> None:
        """Berkas ``.env`` yang ditulis untuk ``guard serve`` tidak memuat kunci.

        Menaruhnya di sana akan melanggar pemisahan yang justru sedang diuji:
        subproses Guard membaca berkas itu seluruhnya.
        """
        teks = (EVALS / "environment.py").read_text(encoding="utf-8")
        modul = ast.parse(teks)

        ditulis: list[str] = []
        for node in ast.walk(modul):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if EVAL_API_KEY_VAR in node.value and "tidak" not in node.value.lower():
                    ditulis.append(node.value[:60])
        # Satu-satunya penyebutan yang sah adalah di docstring, yang
        # menjelaskan bahwa kunci itu TIDAK ditulis ke sana.
        assert ditulis == []

    def test_only_three_guard_paths_are_appended(self) -> None:
        """Yang ditambahkan ke env Guard hanya tiga path, dan semuanya path."""
        teks = (EVALS / "environment.py").read_text(encoding="utf-8")
        for kunci in ("GUARD_MANDATE_PATH", "GUARD_SIGNING_KEY_PATH", "GUARD_PUBLIC_KEYS_PATH"):
            assert kunci in teks
        assert "API_KEY=" not in teks


class TestExampleEnvDocumentsButDoesNotHoldTheKey:
    def test_example_has_an_empty_placeholder(self) -> None:
        """``.env.example`` mendokumentasikan namanya dengan nilai kosong."""
        contoh = REPO / ".env.example"
        if not contoh.exists():
            pytest.skip(".env.example tidak ada")
        baris = [
            b.strip()
            for b in contoh.read_text(encoding="utf-8").splitlines()
            if b.strip().startswith(EVAL_API_KEY_VAR)
        ]
        assert baris, f"{EVAL_API_KEY_VAR} tidak didokumentasikan di .env.example"
        for satu in baris:
            nilai = satu.split("=", 1)[1].strip()
            assert nilai == "", f".env.example memuat nilai untuk {EVAL_API_KEY_VAR}"
