"""Konfigurasi: dua file env yang terpisah, dan objek konfigurasi runtime.

Pemisahan file (M0.1)
---------------------

``.env``
    Lima URL runtime: Guard, Approvals CLI, Executor, Watcher, Reconciler.

``.env.migrator``
    Hanya ``MIGRATOR_DB_URL``, yaitu URL role **pemilik objek**. Dibaca hanya
    oleh ``guard db migrate`` dan ``guard db status``.

:class:`RuntimeConfig` sengaja tidak punya atribut untuk URL migrator, dan
:func:`load_runtime_config` tidak pernah membuka ``.env.migrator``. Jadi tidak
ada jalur di mana URL pemilik ikut termuat ke proses runtime, ikut ter-log,
atau terbawa dump environment runtime.

**Batas yang jujur.** Ini bukan batas keamanan keras. Di Windows izin file
tidak setara ``chmod 600``, dan di OS mana pun semua proses milik user yang
sama bisa membaca kedua file. Yang dicegah pemisahan ini adalah pemuatan dan
kebocoran yang tidak disengaja, bukan penyerang yang sudah memegang user itu.
Lihat Security v1.1 RR-1.

Modul ini tidak pernah menulis ke :data:`os.environ`. Nilai dikembalikan ke
pemanggil, supaya rahasia tidak menyebar ke subproses atau ke dump environment
tanpa diminta.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Final

from payment_guard.db import MigrationError

#: Nama file konfigurasi runtime, relatif terhadap akar repo.
RUNTIME_ENV_FILENAME: Final = ".env"

#: Nama file konfigurasi migrator. Terpisah dengan sengaja.
MIGRATOR_ENV_FILENAME: Final = ".env.migrator"

#: Proses runtime -> nama variabel di ``.env``. Migrator TIDAK ada di sini.
RUNTIME_URL_ENV_VARS: Final[dict[str, str]] = {
    "guard": "GUARD_DB_URL",
    "approver": "APPROVER_DB_URL",
    "executor": "EXECUTOR_DB_URL",
    "watcher": "WATCHER_DB_URL",
    "reconciler": "RECONCILER_DB_URL",
}

#: Variabel URL pemilik objek. Hidup di ``.env.migrator``, bukan di ``.env``.
MIGRATOR_URL_ENV_VAR: Final = "MIGRATOR_DB_URL"

#: Environment variable sementara untuk URL admin bootstrap. Tidak ada di file
#: env mana pun; lihat Security v1.1 I6.
ADMIN_URL_ENV_VAR: Final = "PAYMENT_GUARD_ADMIN_DB_URL"


def read_env_file(path: Path) -> dict[str, str]:
    """Baca pasangan ``KEY=VALUE`` dari sebuah file env. Baris lain diabaikan."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_env_file(path: Path, updates: dict[str, str]) -> list[str]:
    """Perbarui atau tambahkan kunci di file env, pertahankan sisanya.

    Mengembalikan daftar NAMA kunci yang ditulis. Nilainya rahasia dan tidak
    dikembalikan. File dibuat dengan izin terbatas sejauh didukung OS; lihat
    catatan batas di docstring modul.
    """
    if not updates:
        return []

    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in existing_lines:
        is_assignment = "=" in line and not line.strip().startswith("#")
        key = line.split("=", 1)[0].strip() if is_assignment else None
        if key is not None and key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)

    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append("# Ditulis oleh `guard db bootstrap-roles`. Rahasia: jangan di-commit.")
        out.extend(f"{key}={value}" for key, value in remaining.items())

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Windows tidak memetakan mode POSIX; lihat catatan batas di atas.
        pass
    return list(updates)


@dataclass(frozen=True)
class RuntimeConfig:
    """URL database untuk kelima proses runtime.

    Sengaja TIDAK punya atribut untuk ``MIGRATOR_DB_URL``: role pemilik objek
    bisa melepas trigger append-only (Security v1.1 RR-2), jadi URL-nya tidak
    boleh bisa diambil dari konfigurasi runtime sama sekali.
    """

    guard_db_url: str
    approver_db_url: str
    executor_db_url: str
    watcher_db_url: str
    reconciler_db_url: str

    @staticmethod
    def attribute_for(process: str) -> str:
        if process not in RUNTIME_URL_ENV_VARS:
            raise ValueError(f"proses runtime tidak dikenal: {process!r}")
        return f"{process}_db_url"

    def url_for(self, process: str) -> str:
        """URL untuk satu proses runtime.

        Raises:
            ValueError: bila ``process`` bukan proses runtime. ``"migrator"``
                termasuk di sini: ia bukan proses runtime.
        """
        return getattr(self, self.attribute_for(process))


def load_runtime_config(env_file: Path, *, require: bool = True) -> RuntimeConfig:
    """Muat konfigurasi runtime dari ``env_file`` dan environment.

    Environment menang atas file, supaya shell dan CI selalu bisa menimpa.
    ``.env.migrator`` tidak pernah dibuka di sini.

    Args:
        require: bila True, URL yang kosong menggagalkan pemuatan.

    Raises:
        MigrationError: bila ``require`` dan ada URL runtime yang belum diisi.
    """
    from_file = read_env_file(env_file)
    values: dict[str, str] = {}
    missing: list[str] = []
    for process, var in RUNTIME_URL_ENV_VARS.items():
        url = (os.environ.get(var) or from_file.get(var) or "").strip()
        if not url:
            missing.append(var)
        values[RuntimeConfig.attribute_for(process)] = url

    if require and missing:
        raise MigrationError(
            f"URL runtime belum diisi: {', '.join(missing)}. "
            f"Jalankan `guard db bootstrap-roles` untuk menulisnya ke {env_file.name}."
        )
    return RuntimeConfig(**values)


def require_migrator_url(migrator_env_file: Path) -> str:
    """URL migrator, dari environment atau dari ``.env.migrator``.

    Tidak pernah membaca ``.env``: URL pemilik objek dan URL runtime memang
    tidak boleh bercampur dalam satu file.

    Raises:
        MigrationError: bila tidak ada di environment maupun di file. Pesan
            menyebut nama file dan perintah bootstrap, tanpa nilai rahasia.
    """
    url = (os.environ.get(MIGRATOR_URL_ENV_VAR) or "").strip()
    if url:
        return url

    if not migrator_env_file.exists():
        raise MigrationError(
            f"{migrator_env_file.name} tidak ada, jadi {MIGRATOR_URL_ENV_VAR} belum diketahui.\n"
            f"Jalankan `guard db bootstrap-roles` lebih dulu; perintah itu menulis "
            f"{MIGRATOR_URL_ENV_VAR} ke {migrator_env_file.name}.\n"
            f"File dicari di: {migrator_env_file}"
        )

    url = (read_env_file(migrator_env_file).get(MIGRATOR_URL_ENV_VAR) or "").strip()
    if not url:
        raise MigrationError(
            f"{MIGRATOR_URL_ENV_VAR} kosong di {migrator_env_file.name}. "
            "Jalankan `guard db bootstrap-roles` untuk menulisnya ulang."
        )
    return url


def runtime_config_attributes() -> tuple[str, ...]:
    """Nama atribut :class:`RuntimeConfig`. Dipakai tes untuk cek dua arah."""
    return tuple(f.name for f in fields(RuntimeConfig))
