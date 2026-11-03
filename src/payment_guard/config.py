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


# --------------------------------------------------------------------------- #
# Konfigurasi proses Guard
# --------------------------------------------------------------------------- #

#: Variabel environment yang dibaca Guard (Arsitektur v1.1 bagian 2.1).
GUARD_ENV_VARS: Final[dict[str, str]] = {
    "db_url": "GUARD_DB_URL",
    "mandate_path": "GUARD_MANDATE_PATH",
    "signing_key_path": "GUARD_SIGNING_KEY_PATH",
    "public_keys_path": "GUARD_PUBLIC_KEYS_PATH",
    "heartbeat_seconds": "GUARD_HEARTBEAT_SECONDS",
    "lock_timeout_ms": "GUARD_LOCK_TIMEOUT_MS",
}

#: Heartbeat 5 detik, timeout 2 detik (Arsitektur v1.1 bagian 9). Batasnya
#: dijaga: di bawah 1 detik heartbeat membanjiri database, di atas 60 detik
#: Guard bisa menjawab bermenit-menit setelah lock-nya hilang.
DEFAULT_HEARTBEAT_SECONDS: Final = 5
HEARTBEAT_SECONDS_MIN: Final = 1
HEARTBEAT_SECONDS_MAX: Final = 60

#: ``lock_timeout`` dan ``statement_timeout`` untuk transaksi keputusan
#: (Arsitektur v1.1 bagian 4, Security v1.1 D2).
DEFAULT_LOCK_TIMEOUT_MS: Final = 5000
LOCK_TIMEOUT_MS_MIN: Final = 100
LOCK_TIMEOUT_MS_MAX: Final = 600_000


class GuardConfigError(ValueError):
    """Konfigurasi Guard tidak sah. Guard menolak start.

    Pesannya ditampilkan ke operator, jadi ia tidak pernah memuat nilai
    rahasia: URL diredaksi, dan isi berkas kunci tidak pernah dibaca di sini.
    """


@dataclass(frozen=True)
class GuardConfig:
    """Konfigurasi satu proses Guard, sudah divalidasi.

    Tidak punya atribut untuk URL migrator maupun URL proses lain: Guard hanya
    boleh tahu URL-nya sendiri (Arsitektur v1.1 bagian 2).
    """

    db_url: str
    mandate_path: Path
    signing_key_path: Path
    public_keys_path: Path
    heartbeat_seconds: int
    lock_timeout_ms: int


def load_guard_config(env_file: Path, *, repo_root: Path) -> GuardConfig:
    """Muat dan validasi konfigurasi Guard dari environment lalu ``env_file``.

    Environment menang atas berkas, seperti pemuat runtime lain.
    ``.env.migrator`` tidak pernah dibuka: Guard tidak punya urusan dengan URL
    pemilik objek (aturan M0.1).

    Args:
        env_file: berkas ``.env`` runtime.
        repo_root: akar repo, dipakai untuk menolak path kunci di dalamnya.

    Raises:
        GuardConfigError: pada pelanggaran pertama yang ditemukan.
    """
    from_file = read_env_file(env_file)

    def value_of(field: str) -> str:
        var = GUARD_ENV_VARS[field]
        return (os.environ.get(var) or from_file.get(var) or "").strip()

    db_url = value_of("db_url")
    if not db_url:
        raise GuardConfigError(
            f"{GUARD_ENV_VARS['db_url']} belum diisi. Jalankan "
            "`guard db bootstrap-roles` lebih dulu, atau isi di "
            f"{env_file.name}."
        )

    mandate_path = _require_existing_file("mandate_path", value_of("mandate_path"), env_file)
    signing_key_path = _require_existing_file(
        "signing_key_path", value_of("signing_key_path"), env_file
    )
    _require_outside_repo(signing_key_path, repo_root)
    public_keys_path = _require_existing_file(
        "public_keys_path", value_of("public_keys_path"), env_file
    )

    heartbeat_seconds = _require_bounded_int(
        "heartbeat_seconds",
        value_of("heartbeat_seconds"),
        default=DEFAULT_HEARTBEAT_SECONDS,
        minimum=HEARTBEAT_SECONDS_MIN,
        maximum=HEARTBEAT_SECONDS_MAX,
    )
    lock_timeout_ms = _require_bounded_int(
        "lock_timeout_ms",
        value_of("lock_timeout_ms"),
        default=DEFAULT_LOCK_TIMEOUT_MS,
        minimum=LOCK_TIMEOUT_MS_MIN,
        maximum=LOCK_TIMEOUT_MS_MAX,
    )

    return GuardConfig(
        db_url=db_url,
        mandate_path=mandate_path,
        signing_key_path=signing_key_path,
        public_keys_path=public_keys_path,
        heartbeat_seconds=heartbeat_seconds,
        lock_timeout_ms=lock_timeout_ms,
    )


def _require_existing_file(field: str, raw: str, env_file: Path) -> Path:
    var = GUARD_ENV_VARS[field]
    if not raw:
        raise GuardConfigError(f"{var} belum diisi (environment atau {env_file.name})")
    path = Path(raw).expanduser()
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise GuardConfigError(f"{var} bukan path yang bisa dibaca: {exc.strerror}") from None
    if not resolved.exists():
        raise GuardConfigError(f"{var} menunjuk berkas yang tidak ada: {resolved}")
    if not resolved.is_file():
        raise GuardConfigError(f"{var} harus menunjuk berkas, bukan direktori: {resolved}")
    try:
        with resolved.open("rb"):
            pass
    except OSError as exc:
        raise GuardConfigError(f"{var} tidak bisa dibaca ({resolved}): {exc.strerror}") from None
    return resolved


def _require_outside_repo(path: Path, repo_root: Path) -> None:
    """Tolak bahan kunci yang berada di dalam akar repo.

    Berkas di dalam repo bisa ikut ter-commit karena satu ``git add -f``, satu
    aturan ``.gitignore`` yang berubah, atau satu salinan direktori. Menaruhnya
    di luar repo menghapus seluruh kelas kesalahan itu, bukan menguranginya.
    Security v1.1 I1 dan asumsi 2 di bagian 4.
    """
    root = repo_root.resolve()
    if path == root or root in path.parents:
        raise GuardConfigError(
            f"{GUARD_ENV_VARS['signing_key_path']} berada di dalam akar repo: {path}\n"
            f"Akar repo: {root}\n"
            "Bahan kunci harus di luar repo supaya tidak bisa ter-commit karena "
            "kecelakaan. Pindahkan berkasnya, mis. ke direktori sendiri di luar "
            "pohon repo, lalu arahkan variabelnya ke sana."
        )


def _require_bounded_int(field: str, raw: str, *, default: int, minimum: int, maximum: int) -> int:
    var = GUARD_ENV_VARS[field]
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise GuardConfigError(f"{var} harus bilangan bulat, dapat {raw!r}") from None
    if not minimum <= value <= maximum:
        raise GuardConfigError(f"{var} harus {minimum}..{maximum}, dapat {value}")
    return value
