"""``guard db migrate`` dan ``status`` tidak pernah membuka ``.env``.

Ini penjaga regresi, bukan sekadar pernyataan niat. Pemisahan ``.env`` dan
``.env.migrator`` hanya bermakna kalau tidak ada jalur yang membatalkannya,
dan jalur yang paling mungkin muncul nanti adalah seseorang menambahkan
pemuatan ``.env`` ke ``main()`` "supaya semua perintah seragam".

Tiga lapis yang diuji, dari yang paling kuat:

1. **Pembacaan berkas nyata.** ``Path.read_text`` dan ``Path.read_bytes``
   diintip; tidak boleh ada berkas bernama ``.env`` yang terbaca.
2. **Pemuat env.** ``config.read_env_file`` diintip; tidak boleh dipanggil
   dengan path ``.env``.
3. **Perilaku.** ``.env`` yang berisi ``MIGRATOR_DB_URL`` yang kelihatan sah
   tidak boleh membuat perintah berhasil, dan pesan errornya hanya menyebut
   ``.env.migrator``.

Tes ini unit, bukan integrasi: ketiga perintah gagal sebelum menyentuh
database, jadi tidak perlu Postgres.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from payment_guard import config
from payment_guard.cli import build_parser, main
from payment_guard.config import (
    ADMIN_URL_ENV_VAR,
    MIGRATOR_ENV_FILENAME,
    MIGRATOR_URL_ENV_VAR,
    RUNTIME_ENV_FILENAME,
    RUNTIME_URL_ENV_VARS,
)
from payment_guard.migrate import migrations_dir

#: URL palsu di ``.env`` yang BENTUKNYA sah. Kalau perintah berhasil atau URL
#: ini terbaca, berarti ``.env`` ikut dimuat.
SENTINEL_URL = "postgresql://migrator_owner:sentinel-jangan-terbaca@127.0.0.1:5434/payment_guard"

#: ``.env`` diikuti apa pun selain ``.migrator``. Dipakai supaya pemeriksaan
#: pesan tidak salah tangkap kata ``.env.migrator`` yang memuat ``.env``.
BARE_ENV_MENTION = re.compile(r"\.env(?!\.migrator)")

COMMANDS = ["migrate", "status"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment kosong: tidak ada override yang menolong."""
    for name in (*RUNTIME_URL_ENV_VARS.values(), MIGRATOR_URL_ENV_VAR, ADMIN_URL_ENV_VAR):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def staged(tmp_path: Path) -> tuple[Path, Path]:
    """``.env`` yang ADA dan berisi URL migrator, di sebelah ``.env.migrator`` yang TIDAK ada.

    Susunan ini adalah perangkapnya: kalau migrate memuat ``.env``, ia akan
    menemukan URL yang kelihatan sah dan lanjut.
    """
    env_file = tmp_path / RUNTIME_ENV_FILENAME
    env_file.write_text(
        f"{MIGRATOR_URL_ENV_VAR}={SENTINEL_URL}\nGUARD_DB_URL={SENTINEL_URL}\n",
        encoding="utf-8",
        newline="\n",
    )
    migrator_file = tmp_path / MIGRATOR_ENV_FILENAME
    assert not migrator_file.exists()
    return env_file, migrator_file


class ReadSpy:
    """Catat setiap pembacaan berkas selama satu perintah."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.paths: list[Path] = []

        real_read_text = Path.read_text
        real_read_bytes = Path.read_bytes
        real_read_env_file = config.read_env_file

        def spy_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
            self.paths.append(path)
            return real_read_text(path, *args, **kwargs)

        def spy_read_bytes(path: Path) -> bytes:
            self.paths.append(path)
            return real_read_bytes(path)

        def spy_read_env_file(path: Path) -> dict[str, str]:
            self.paths.append(path)
            return real_read_env_file(path)

        monkeypatch.setattr(Path, "read_text", spy_read_text)
        monkeypatch.setattr(Path, "read_bytes", spy_read_bytes)
        monkeypatch.setattr(config, "read_env_file", spy_read_env_file)

    def names(self) -> list[str]:
        return [p.name for p in self.paths]

    def runtime_env_reads(self) -> list[str]:
        return [str(p) for p in self.paths if p.name == RUNTIME_ENV_FILENAME]


@pytest.mark.parametrize("command", COMMANDS)
def test_runtime_env_file_is_never_read(
    command: str, staged: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lapis 1 dan 2: tidak ada pembacaan berkas bernama ``.env``."""
    _env_file, migrator_file = staged
    spy = ReadSpy(monkeypatch)

    code = main(
        [
            "db",
            command,
            "--migrations-dir",
            str(migrations_dir()),
            "--migrator-env-file",
            str(migrator_file),
        ]
    )

    assert code == 1, f"{command} berhasil padahal .env.migrator tidak ada"
    assert spy.runtime_env_reads() == [], (
        f"{command} membaca .env: {spy.runtime_env_reads()}. "
        "Pemisahan .env dan .env.migrator dibatalkan oleh jalur ini."
    )


@pytest.mark.parametrize("command", COMMANDS)
def test_sentinel_url_in_runtime_env_does_not_make_it_succeed(
    command: str, staged: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Lapis 3: URL yang bentuknya sah di ``.env`` tidak menolong."""
    _env_file, migrator_file = staged

    code = main(
        [
            "db",
            command,
            "--migrations-dir",
            str(migrations_dir()),
            "--migrator-env-file",
            str(migrator_file),
        ]
    )
    captured = capsys.readouterr()

    assert code == 1
    assert "sentinel-jangan-terbaca" not in captured.err
    assert "sentinel-jangan-terbaca" not in captured.out


@pytest.mark.parametrize("command", COMMANDS)
def test_error_message_names_only_the_migrator_file(
    command: str, staged: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Pesan error menyebut ``.env.migrator``, dan tidak pernah ``.env`` saja."""
    _env_file, migrator_file = staged

    main(
        [
            "db",
            command,
            "--migrations-dir",
            str(migrations_dir()),
            "--migrator-env-file",
            str(migrator_file),
        ]
    )
    err = capsys.readouterr().err

    assert MIGRATOR_ENV_FILENAME in err
    assert "guard db bootstrap-roles" in err
    stray = BARE_ENV_MENTION.findall(err)
    assert stray == [], f"pesan menyebut .env runtime: {err!r}"


@pytest.mark.parametrize("command", COMMANDS)
def test_namespace_has_no_runtime_env_attribute(command: str) -> None:
    """migrate dan status tidak punya ``env_file`` sama sekali.

    Tanpa atribut itu, tidak ada path ``.env`` yang bisa mereka baca walau
    tidak sengaja. Bootstrap tetap punya, karena ia yang menulisnya.
    """
    args = build_parser().parse_args(["db", command])
    assert not hasattr(args, "env_file")
    assert hasattr(args, "migrator_env_file")

    bootstrap_args = build_parser().parse_args(["db", "bootstrap-roles"])
    assert hasattr(bootstrap_args, "env_file")


def test_spy_would_catch_a_regression(
    staged: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pembuktian bahwa penjaganya bukan tes kosong.

    Pembacaan ``.env`` ditanam secara eksplisit; spy harus melihatnya. Tanpa
    tes ini, spy yang diam-diam berhenti bekerja akan membuat ketiga tes di
    atas lulus selamanya.
    """
    env_file, _migrator_file = staged
    spy = ReadSpy(monkeypatch)

    env_file.read_text(encoding="utf-8")

    assert spy.runtime_env_reads() == [str(env_file)]
    assert RUNTIME_ENV_FILENAME in spy.names()
