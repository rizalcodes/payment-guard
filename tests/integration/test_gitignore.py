"""``.gitignore`` diuji lewat ``git`` yang sesungguhnya, bukan lewat pencocokan teks.

Pola ignore punya aturan presedensi (negasi, urutan, direktori) yang tidak
tertangkap oleh ``pattern in file_text``. Yang penting di sini adalah jawaban
git, karena gitlah yang memutuskan apakah rahasia ikut ter-commit.

Ditandai ``integration`` karena menjalankan proses ``git``, bukan karena butuh
database.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]

#: File rahasia yang WAJIB diabaikan git.
#:
#: ``.env.test`` ikut di sini sejak M5.1: ia memuat URL admin cluster tes, yang
#: memegang CREATEROLE di cluster itu.
MUST_BE_IGNORED = (".env", ".env.migrator", ".env.test")

#: File contoh yang WAJIB bisa di-commit.
#:
#: Daftar ini tertutup, dan ``test_only_example_env_files_are_tracked`` membaca
#: **indeks git** untuk membandingkannya. Itu berarti berkas ``.env*`` baru
#: lolos selama ia masih untracked dan baru gagal setelah di-commit — yang
#: persis terjadi pada ``.env.test.example`` di M5.1: suite dijalankan sebelum
#: commit, lalu berkasnya masuk indeks dan tes ini mulai gagal di master.
#: Menambahkan berkas ``.env*`` berarti menambahkannya ke salah satu daftar di
#: sini, pada commit yang sama.
MUST_BE_TRACKABLE = (
    ".env.example",
    ".env.migrator.example",
    ".env.test.example",
    # Templat devnet lokal. Ditambahkan di sini SETELAH commit yang
    # menambahkannya ke indeks, bukan pada commit yang sama — persis kesalahan
    # yang diperingatkan komentar di atas, diulangi sekali lagi. Lihat catatan
    # M6.devnet di docs/CHANGELOG.md.
    ".env.devnet.example",
)


@pytest.fixture(scope="module", autouse=True)
def git_available() -> None:
    if shutil.which("git") is None:
        pytest.skip("git tidak ada di PATH")
    if not (ROOT / ".git").exists():
        pytest.skip("belum ada repo git di akar proyek")


def git(*args: str) -> subprocess.CompletedProcess[str]:
    """Jalankan git di akar repo.

    S603/S607 dimatikan di sini dengan sengaja: argumennya konstanta di berkas
    ini (bukan input), dan `git` memang harus dicari lewat PATH karena lokasi
    pemasangannya berbeda antar mesin.
    """
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


class TestSecretFilesAreIgnored:
    @pytest.mark.parametrize("name", MUST_BE_IGNORED)
    def test_check_ignore_reports_a_rule(self, name: str) -> None:
        """``git check-ignore -v`` harus menyebut aturan yang mengabaikannya."""
        result = git("check-ignore", "-v", name)
        assert result.returncode == 0, f"{name} TIDAK diabaikan git"
        assert ".gitignore" in result.stdout, result.stdout
        assert name in result.stdout

    @pytest.mark.parametrize("name", MUST_BE_IGNORED)
    def test_git_refuses_to_add_it(self, tmp_path: Path, name: str) -> None:
        """Uji definitif: ``git add`` menolak, dengan atau tanpa file di disk.

        ``--dry-run`` dipakai supaya indeks repo tidak ikut berubah.
        """
        result = git("add", "--dry-run", name)
        assert result.returncode != 0 or "ignored" in (result.stdout + result.stderr).lower(), (
            f"git bersedia menambahkan {name}: {result.stdout} {result.stderr}"
        )


class TestExampleFilesAreTrackable:
    @pytest.mark.parametrize("name", MUST_BE_TRACKABLE)
    def test_not_ignored(self, name: str) -> None:
        assert (ROOT / name).is_file(), f"{name} tidak ada di disk"
        result = git("check-ignore", "--no-index", "-q", name)
        # Keluar 1 berarti "tidak diabaikan", yang justru yang diinginkan.
        assert result.returncode == 1, f"{name} diabaikan git padahal harus bisa di-commit"

    @pytest.mark.parametrize("name", MUST_BE_TRACKABLE)
    def test_is_actually_tracked(self, name: str) -> None:
        result = git("ls-files", "--", name)
        assert result.stdout.strip() == name, f"{name} belum masuk indeks git"


class TestNoSecretsInTheIndex:
    def test_only_example_env_files_are_tracked(self) -> None:
        """Satu-satunya berkas ber-awalan .env di indeks adalah contoh."""
        result = git("ls-files")
        tracked = [
            line for line in result.stdout.splitlines() if Path(line).name.startswith(".env")
        ]
        assert sorted(tracked) == sorted(MUST_BE_TRACKABLE), tracked

    @pytest.mark.parametrize(
        "name",
        [
            # Bentuk berkas kunci yang didaftar .gitignore. Daftar ini sengaja
            # hanya memuat yang memang dideklarasikan: kunci SSH (id_rsa dan
            # kawan-kawan) TIDAK tercakup, dan yang menahannya adalah hook
            # pre-commit `detect-private-key`, yang bekerja atas isi berkas.
            "berkas_uji.key",
            "berkas_uji.pem",
            "berkas_uji.p8",
            "berkas_uji.jwk",
            "berkas_uji.keystore",
            "guard_signing.json",
            "executor_wallet.json",
            "secrets/apa_saja.txt",
            "keys/apa_saja.txt",
        ],
    )
    def test_key_file_shapes_are_ignored(self, name: str) -> None:
        result = git("check-ignore", "-v", name)
        assert result.returncode == 0, f"{name} tidak diabaikan git"

    def test_private_key_hook_covers_what_gitignore_does_not(self) -> None:
        """Lapis kedua untuk kunci yang namanya tidak terduga, mis. id_rsa."""
        text = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        assert "detect-private-key" in text
        assert "gitleaks" in text

    def test_no_key_like_file_is_tracked(self) -> None:
        result = git("ls-files")
        suspicious = [
            line
            for line in result.stdout.splitlines()
            if line.endswith((".key", ".pem", ".p8", ".pk8", ".jwk", ".keystore"))
        ]
        assert suspicious == [], suspicious

    def test_eval_results_are_ignored(self) -> None:
        result = git("check-ignore", "-v", "evals/results/transkrip.json")
        assert result.returncode == 0
