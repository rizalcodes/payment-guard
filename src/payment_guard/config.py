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
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Final

from payment_guard.addresses import AddressError, normalize_address
from payment_guard.chains import (
    NETWORK_ENV_VAR,
    NETWORK_PRODUCTION,
    NetworkError,
    expected_chain_id_for_network,
    normalize_network,
)
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
    #: Jaringan yang dipilih konfigurasi. ``base-sepolia`` bila tidak disetel.
    #:
    #: Guard memakainya dua kali: saat memuat mandate (nama chain mana yang
    #: sah) dan saat menandatangani (``chain_id`` mana yang masuk envelope).
    network: str = NETWORK_PRODUCTION


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

    mandate_path = _require_existing_file(
        GUARD_ENV_VARS["mandate_path"], value_of("mandate_path"), env_file
    )
    signing_key_path = _require_existing_file(
        GUARD_ENV_VARS["signing_key_path"], value_of("signing_key_path"), env_file
    )
    _require_outside_repo(GUARD_ENV_VARS["signing_key_path"], signing_key_path, repo_root)
    public_keys_path = _require_existing_file(
        GUARD_ENV_VARS["public_keys_path"], value_of("public_keys_path"), env_file
    )

    heartbeat_seconds = _require_bounded_int(
        GUARD_ENV_VARS["heartbeat_seconds"],
        value_of("heartbeat_seconds"),
        default=DEFAULT_HEARTBEAT_SECONDS,
        minimum=HEARTBEAT_SECONDS_MIN,
        maximum=HEARTBEAT_SECONDS_MAX,
    )
    lock_timeout_ms = _require_bounded_int(
        GUARD_ENV_VARS["lock_timeout_ms"],
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
        network=_load_network(from_file),
    )


#: Variabel yang dibaca Approvals CLI (M4).
#:
#: URL-nya ``APPROVER_DB_URL``, bukan ``GUARD_DB_URL``: approve, reject, dan
#: kedaluwarsa adalah hak role approver di ``0002_roles.sql``, dan hanya role
#: itu yang boleh menulis ``approved_at``, ``valid_until``, serta ``envelope``.
#:
#: Path kuncinya variabel yang sama dengan milik Guard. Itu disengaja: yang
#: menandatangani tetap kunci Guard, dan Approvals CLI meminjamnya untuk satu
#: keputusan (Arsitektur v1.1 bagian 7). Tidak ada kunci kedua yang harus
#: dirotasi, dicabut, dan dilacak sendiri.
APPROVALS_ENV_VARS: Final[dict[str, str]] = {
    "db_url": RUNTIME_URL_ENV_VARS["approver"],
    "signing_key_path": GUARD_ENV_VARS["signing_key_path"],
    "lock_timeout_ms": GUARD_ENV_VARS["lock_timeout_ms"],
}


@dataclass(frozen=True)
class ApprovalsConfig:
    """Konfigurasi Approvals CLI, sudah divalidasi.

    Tidak punya atribut untuk URL proses lain maupun untuk mandate: agent yang
    relevan dibaca dari ``active_mandates``, bukan dari berkas di disk, karena
    yang mengikat keputusan adalah mandate yang benar-benar dimuat Guard
    (Arsitektur v1.1 bagian 5.4).
    """

    db_url: str
    signing_key_path: Path
    lock_timeout_ms: int
    #: Jaringan yang dipilih konfigurasi. ``base-sepolia`` bila tidak disetel.
    #:
    #: Approvals CLI menandatangani envelope di jalur ``approved_by_human``,
    #: jadi ia butuh jaringan yang sama dengan Guard: tanpa ini, persetujuan
    #: manusia di devnet menghasilkan ``chain_id`` 84532 untuk transaksi yang
    #: dikirim ke chain 31337, dan Executor menolaknya di cek 2.
    network: str = NETWORK_PRODUCTION


def load_approvals_config(env_file: Path, *, repo_root: Path) -> ApprovalsConfig:
    """Muat konfigurasi Approvals CLI dari environment lalu ``env_file``.

    ``.env.migrator`` tidak pernah dibuka, sama seperti pemuat runtime lain.

    Args:
        env_file: berkas ``.env`` runtime.
        repo_root: akar repo, dipakai untuk menolak path kunci di dalamnya.

    Raises:
        GuardConfigError: pada pelanggaran pertama yang ditemukan.
    """
    from_file = read_env_file(env_file)

    def value_of(field: str) -> str:
        var = APPROVALS_ENV_VARS[field]
        return (os.environ.get(var) or from_file.get(var) or "").strip()

    db_url = value_of("db_url")
    if not db_url:
        raise GuardConfigError(
            f"{APPROVALS_ENV_VARS['db_url']} belum diisi. Jalankan "
            f"`guard db bootstrap-roles` lebih dulu, atau isi di {env_file.name}."
        )

    signing_key_path = _require_existing_file(
        APPROVALS_ENV_VARS["signing_key_path"], value_of("signing_key_path"), env_file
    )
    _require_outside_repo(APPROVALS_ENV_VARS["signing_key_path"], signing_key_path, repo_root)

    lock_timeout_ms = _require_bounded_int(
        APPROVALS_ENV_VARS["lock_timeout_ms"],
        value_of("lock_timeout_ms"),
        default=DEFAULT_LOCK_TIMEOUT_MS,
        minimum=LOCK_TIMEOUT_MS_MIN,
        maximum=LOCK_TIMEOUT_MS_MAX,
    )

    return ApprovalsConfig(
        db_url=db_url,
        signing_key_path=signing_key_path,
        lock_timeout_ms=lock_timeout_ms,
        network=_load_network(from_file),
    )


# --------------------------------------------------------------------------- #
# Konfigurasi proses Executor (M5)
# --------------------------------------------------------------------------- #

#: Variabel yang dibaca Executor (PRD v2 bagian 9, Arsitektur v1.1 bagian 2.1).
#:
#: Perhatikan yang **tidak** ada di sini: ``GUARD_SIGNING_KEY_PATH``. Executor
#: hanya memegang kunci publik Guard, supaya ia bisa memeriksa persetujuan
#: tanpa pernah bisa membuatnya (Arsitektur v1.1 bagian 2). Ada tes AST yang
#: memeriksa nama variabel itu tidak muncul di modul Executor sama sekali.
#:
#: ``EXECUTOR_KEY_PATH`` adalah kunci wallet milik Executor sendiri. Ia ditolak
#: bila berada di dalam akar repo, dengan pemeriksaan dan alasan yang sama
#: seperti kunci penanda tangan Guard (Security v1.1 I1).
EXECUTOR_ENV_VARS: Final[dict[str, str]] = {
    "db_url": RUNTIME_URL_ENV_VARS["executor"],
    "wallet_key_path": "EXECUTOR_KEY_PATH",
    "public_keys_path": GUARD_ENV_VARS["public_keys_path"],
    "rpc_url": "RPC_URL",
    "chain_id": "CHAIN_ID",
    "usdc_address": "USDC_ADDRESS",
    "lock_timeout_ms": "EXECUTOR_LOCK_TIMEOUT_MS",
}

#: Endpoint RPC publik Base Sepolia.
#:
#: Diverifikasi 4 November 2026 ke dokumentasi Base, halaman "Connect to Base"
#: (https://docs.base.org/get-started/connect-to-base), yang menyebut
#: ``https://sepolia.base.org`` dengan chain id ``84532``.
#:
#: URL ini **tanpa kunci API**, dan itu syarat, bukan kebetulan: nilai bawaan
#: ikut ter-commit, jadi ia tidak boleh pernah berupa URL berkunci. Operator
#: yang memakai penyedia berbayar menaruh URL-nya di ``.env``, yang
#: di-gitignore, dan Executor tidak pernah mencetak URL itu utuh; lihat
#: :func:`payment_guard.db.endpoint_host`.
DEFAULT_RPC_URL: Final = "https://sepolia.base.org"

#: Kontrak USDC testnet di Base Sepolia.
#:
#: Diverifikasi 4 November 2026 ke dokumentasi Circle, halaman "USDC contract
#: addresses" (https://developers.circle.com/stablecoins/usdc-contract-addresses),
#: baris tabel testnet "Base Sepolia".
#:
#: Disimpan lowercase supaya cocok dengan bentuk ternormalisasi di seluruh
#: sistem. Nilai ini tidak dipercaya buta: Executor memanggil ``decimals()``
#: pada kontrak ini saat start dan menolak jalan bila hasilnya bukan 6, jadi
#: alamat yang salah tertangkap sebelum ada transaksi yang dibangun.
DEFAULT_USDC_ADDRESS: Final = "0x036cbd53842c5426634e7929541ec2318f3dcf7e"

#: Skema URL RPC yang diterima. Daftar tertutup, dan hanya HTTP: klien di
#: :class:`payment_guard.chain.Web3Client` memakai ``HTTPProvider``, jadi
#: menerima ``ws://`` di sini akan menghasilkan konfigurasi yang lolos validasi
#: lalu gagal saat dipakai. ``file://`` dan skema lain tidak punya arti untuk
#: endpoint JSON-RPC.
RPC_URL_SCHEMES: Final[tuple[str, ...]] = ("https://", "http://")

#: Batas atas ``chain_id``; kolom ``decisions.chain_id`` adalah ``INT``.
_CHAIN_ID_MAX: Final = 2**31 - 1


@dataclass(frozen=True)
class ExecutorConfig:
    """Konfigurasi satu proses Executor, sudah divalidasi.

    Tidak punya atribut untuk kunci penanda tangan Guard, untuk URL proses
    lain, maupun untuk mandate: Executor memeriksa envelope dengan kunci
    publik, dan mandate yang berlaku dibaca dari ``active_mandates``.
    """

    db_url: str
    wallet_key_path: Path
    public_keys_path: Path
    rpc_url: str
    chain_id: int
    usdc_address: str
    lock_timeout_ms: int
    #: Jaringan yang dipilih konfigurasi. ``base-sepolia`` bila tidak disetel.
    #:
    #: Dibawa di config, bukan dibaca ulang dari environment di tempat lain:
    #: dua pembacaan bisa berbeda, dan yang satu akan memutuskan sesuatu yang
    #: tidak dilihat yang lain.
    network: str = NETWORK_PRODUCTION


def load_executor_config(env_file: Path, *, repo_root: Path) -> ExecutorConfig:
    """Muat dan validasi konfigurasi Executor dari environment lalu ``env_file``.

    ``.env.migrator`` tidak pernah dibuka, seperti pemuat runtime lain.

    Args:
        env_file: berkas ``.env`` runtime.
        repo_root: akar repo, dipakai untuk menolak kunci wallet di dalamnya.

    Raises:
        GuardConfigError: pada pelanggaran pertama yang ditemukan.
    """
    from_file = read_env_file(env_file)

    def value_of(field: str) -> str:
        var = EXECUTOR_ENV_VARS[field]
        return (os.environ.get(var) or from_file.get(var) or "").strip()

    db_url = value_of("db_url")
    if not db_url:
        raise GuardConfigError(
            f"{EXECUTOR_ENV_VARS['db_url']} belum diisi. Jalankan "
            f"`guard db bootstrap-roles` lebih dulu, atau isi di {env_file.name}."
        )

    wallet_key_path = _require_existing_file(
        EXECUTOR_ENV_VARS["wallet_key_path"], value_of("wallet_key_path"), env_file
    )
    _require_outside_repo(EXECUTOR_ENV_VARS["wallet_key_path"], wallet_key_path, repo_root)
    public_keys_path = _require_existing_file(
        EXECUTOR_ENV_VARS["public_keys_path"], value_of("public_keys_path"), env_file
    )

    rpc_url = value_of("rpc_url") or DEFAULT_RPC_URL
    if not rpc_url.startswith(RPC_URL_SCHEMES):
        raise GuardConfigError(
            f"{EXECUTOR_ENV_VARS['rpc_url']} harus dimulai dengan salah satu dari: "
            f"{', '.join(RPC_URL_SCHEMES)}"
        )

    network = _load_network(from_file)
    expected_chain_id = expected_chain_id_for_network(network)
    chain_id = _require_bounded_int(
        EXECUTOR_ENV_VARS["chain_id"],
        value_of("chain_id"),
        default=expected_chain_id,
        minimum=1,
        maximum=_CHAIN_ID_MAX,
    )
    if chain_id != expected_chain_id:
        raise GuardConfigError(
            f"{EXECUTOR_ENV_VARS['chain_id']} harus {expected_chain_id} "
            f"untuk jaringan {network!r}, dapat {chain_id}. Satu jaringan punya "
            "tepat satu chain; lihat payment_guard.chains."
        )

    raw_usdc = value_of("usdc_address") or DEFAULT_USDC_ADDRESS
    try:
        usdc_address = normalize_address(raw_usdc)
    except AddressError as exc:
        raise GuardConfigError(f"{EXECUTOR_ENV_VARS['usdc_address']} tidak sah: {exc}") from None

    lock_timeout_ms = _require_bounded_int(
        EXECUTOR_ENV_VARS["lock_timeout_ms"],
        value_of("lock_timeout_ms"),
        default=DEFAULT_LOCK_TIMEOUT_MS,
        minimum=LOCK_TIMEOUT_MS_MIN,
        maximum=LOCK_TIMEOUT_MS_MAX,
    )

    return ExecutorConfig(
        db_url=db_url,
        wallet_key_path=wallet_key_path,
        public_keys_path=public_keys_path,
        rpc_url=rpc_url,
        chain_id=chain_id,
        usdc_address=usdc_address,
        lock_timeout_ms=lock_timeout_ms,
        network=network,
    )


# --------------------------------------------------------------------------- #
# Watcher dan Reconciler (M6)
# --------------------------------------------------------------------------- #

#: Variabel Watcher. Arsitektur v1.1 bagian 2.1 menyebut ``RPC_URL``,
#: ``WATCHER_DB_URL``, ``CHAIN_ID``, ``USDC_ADDRESS``, ``CONFIRMATIONS``,
#: ``SCAN_BATCH_SIZE``, dan ``REORG_LOOKBACK``.
#:
#: Dua variabel di bawah **tidak** ada di tabel itu, dan itu disengaja serta
#: dicatat (``docs/decisions.md``, M6):
#:
#: * ``EXECUTOR_WALLET_ADDRESS`` — PRD v2 bagian 10 mewajibkan Watcher
#:   menyaring ``from = alamat wallet Executor``, tetapi tabel konfigurasi
#:   tidak punya cara bagi Watcher untuk mengetahui alamat itu. Ia **tidak**
#:   boleh membaca berkas kunci wallet (Arsitektur bagian 2: "Kedua key" ada di
#:   kolom yang tidak boleh dipakai Watcher), dan ia tidak punya SELECT pada
#:   ``executions`` (matriks bagian 3.3). Alamat adalah informasi publik; yang
#:   rahasia adalah kuncinya. Jadi alamatnya dikonfigurasi, tanpa default:
#:   default apa pun di sini berarti memantau wallet yang salah dalam diam.
#: * ``WATCHER_INTERVAL_SECONDS`` — jeda antar siklus untuk ``guard watch``
#:   sebagai proses panjang. Dokumen menyebut Watcher "proses panjang" tanpa
#:   menyebut jedanya.
WATCHER_ENV_VARS: Final[dict[str, str]] = {
    "db_url": "WATCHER_DB_URL",
    "rpc_url": "RPC_URL",
    "chain_id": "CHAIN_ID",
    "usdc_address": "USDC_ADDRESS",
    "wallet_address": "EXECUTOR_WALLET_ADDRESS",
    "confirmations": "CONFIRMATIONS",
    "scan_batch_size": "SCAN_BATCH_SIZE",
    "reorg_lookback": "REORG_LOOKBACK",
    "interval_seconds": "WATCHER_INTERVAL_SECONDS",
    "lock_timeout_ms": "WATCHER_LOCK_TIMEOUT_MS",
}

#: Variabel Reconciler. Arsitektur bagian 2.1 hanya menyebut
#: ``RECONCILER_DB_URL``: Reconciler tidak menyentuh chain sama sekali, ia
#: membandingkan tabel dengan tabel.
#:
#: ``RECONCILE_DEADLINE_SECONDS`` tidak ada di tabel itu. PRD v2 bagian 11
#: menulis "setelah batas waktu" tanpa angka, dan batas itu harus punya nilai
#: sebelum ``executed_not_settled`` bisa diputuskan. Lihat ``docs/decisions.md``.
RECONCILER_ENV_VARS: Final[dict[str, str]] = {
    "db_url": "RECONCILER_DB_URL",
    "chain_id": "CHAIN_ID",
    "deadline_seconds": "RECONCILE_DEADLINE_SECONDS",
    "lock_timeout_ms": "RECONCILER_LOCK_TIMEOUT_MS",
}

#: Kedalaman konfirmasi sebelum settlement boleh menjadi ``final``.
#:
#: 12 adalah nilai di Arsitektur bagian 2.1. Batas bawahnya **1**, bukan 0:
#: nol berarti blok terakhir langsung dianggap final, dan itu menghapus satu-
#: satunya hal yang membuat status ``final`` berarti apa pun.
DEFAULT_CONFIRMATIONS: Final = 12
CONFIRMATIONS_MIN: Final = 1
CONFIRMATIONS_MAX: Final = 1000

#: Lebar **awal** satu rentang ``eth_getLogs``.
#:
#: Awal, bukan tetap: node publik menolak rentang yang terlalu lebar, dan
#: :func:`payment_guard.watcher.scan_once` membagi rentangnya dua lalu mencoba
#: lagi sampai :data:`SCAN_RANGE_FLOOR`. Angka di sini karena itu adalah
#: tebakan optimis, bukan batas keras.
#:
#: 1000, diturunkan dari 2000 di M6.1. Rentang yang lebih kecil lebih sering
#: diterima node publik pada percobaan pertama, dan pembagian dua hanya
#: menolong setelah satu permintaan sudah ditolak — yaitu setelah satu
#: perjalanan jaringan terbuang.
DEFAULT_SCAN_BATCH_SIZE: Final = 1000

#: Batas bawah lebar rentang. Di bawah ini, pembagian dua berhenti menolong:
#: node yang menolak 10 blok sedang menolak karena hal lain.
SCAN_RANGE_FLOOR: Final = 10

#: Lebar rentang tidak boleh di bawah lantainya, kalau tidak lantai itu tidak
#: pernah tercapai dan pembagian dua menjadi kode yang tidak pernah berguna.
SCAN_BATCH_SIZE_MIN: Final = SCAN_RANGE_FLOOR
SCAN_BATCH_SIZE_MAX: Final = 10_000

#: Berapa kali satu rentang dicoba, termasuk percobaan pertama.
#:
#: Hanya penolakan yang **tentang lebar rentang** yang memicu percobaan
#: berikutnya, dan setiap percobaan memakai rentang separuh lebih kecil. Dari
#: 1000 sampai 10 butuh tujuh pembagian, jadi 10 memberi kelonggaran tanpa
#: pernah menjadi jalur menunggu tanpa batas.
#:
#: Tidak punya variabel environment, dan itu disengaja. Ia bukan tombol
#: penyetelan: pembagian dua sudah berhenti sendiri di lantainya, dan batas ini
#: hanya jaring terakhir supaya tidak ada perulangan yang bisa berjalan lama.
#: Menambahkannya ke ``.env`` berarti satu nilai lagi yang bisa diisi salah
#: tanpa ada yang memeriksanya.
DEFAULT_SCAN_MAX_ATTEMPTS: Final = 10
SCAN_MAX_ATTEMPTS_MIN: Final = 1
SCAN_MAX_ATTEMPTS_MAX: Final = 20

#: Jendela mundur K untuk memeriksa ulang baris yang sudah ``final``.
#:
#: Harus lebih besar dari ``CONFIRMATIONS``, kalau tidak jendelanya tidak
#: pernah memuat satu pun baris final dan transisi ``final -> reorged`` menjadi
#: kode mati. Pemeriksaannya ada di :func:`load_watcher_config`.
DEFAULT_REORG_LOOKBACK: Final = 64
REORG_LOOKBACK_MIN: Final = 1
REORG_LOOKBACK_MAX: Final = 100_000

#: Jeda antar siklus ``guard watch --loop``, dalam detik.
DEFAULT_WATCHER_INTERVAL_SECONDS: Final = 15
WATCHER_INTERVAL_SECONDS_MIN: Final = 1
WATCHER_INTERVAL_SECONDS_MAX: Final = 3600

#: Berapa lama execution yang sudah disiarkan boleh belum punya settlement
#: ``final`` sebelum dicatat ``executed_not_settled``.
#:
#: Satu jam. Pendek berarti alarm palsu setiap kali chain melambat; panjang
#: berarti pembayaran yang benar-benar hilang baru terlihat besok. Nilainya
#: dikonfigurasi justru karena tidak ada angka yang benar untuk semua orang.
DEFAULT_RECONCILE_DEADLINE_SECONDS: Final = 3600
RECONCILE_DEADLINE_SECONDS_MIN: Final = 60
RECONCILE_DEADLINE_SECONDS_MAX: Final = 7 * 24 * 3600


@dataclass(frozen=True)
class WatcherConfig:
    """Konfigurasi satu proses Watcher, sudah divalidasi.

    Tidak punya atribut untuk kunci apa pun. Watcher membaca chain dan menulis
    ``settlements``; ia tidak menandatangani dan tidak menyiarkan, jadi tidak
    ada alasan baginya memegang bahan kunci (Arsitektur v1.1 bagian 2).
    """

    db_url: str
    rpc_url: str
    chain_id: int
    usdc_address: str
    wallet_address: str
    confirmations: int
    scan_batch_size: int
    reorg_lookback: int
    interval_seconds: int
    lock_timeout_ms: int
    #: Batas percobaan per rentang ``eth_getLogs``. Tanpa variabel environment;
    #: lihat :data:`DEFAULT_SCAN_MAX_ATTEMPTS`.
    scan_max_attempts: int = DEFAULT_SCAN_MAX_ATTEMPTS


@dataclass(frozen=True)
class ReconcilerConfig:
    """Konfigurasi satu siklus Reconciler, sudah divalidasi.

    Tanpa ``rpc_url``: Reconciler membandingkan ``settlements`` dengan
    ``executions`` dan ``decisions``, dan ketiganya ada di database. Bertanya
    ke chain di sini akan berarti dua sumber kebenaran untuk hal yang sama
    (Arsitektur v1.1 prinsip 2).
    """

    db_url: str
    chain_id: int
    deadline_seconds: int
    lock_timeout_ms: int


def load_watcher_config(env_file: Path) -> WatcherConfig:
    """Muat dan validasi konfigurasi Watcher dari environment lalu ``env_file``.

    Raises:
        GuardConfigError: pada pelanggaran pertama yang ditemukan.
    """
    from_file = read_env_file(env_file)

    def value_of(field: str) -> str:
        var = WATCHER_ENV_VARS[field]
        return (os.environ.get(var) or from_file.get(var) or "").strip()

    db_url = value_of("db_url")
    if not db_url:
        raise GuardConfigError(
            f"{WATCHER_ENV_VARS['db_url']} belum diisi. Jalankan "
            f"`guard db bootstrap-roles` lebih dulu, atau isi di {env_file.name}."
        )

    rpc_url = value_of("rpc_url") or DEFAULT_RPC_URL
    if not rpc_url.startswith(RPC_URL_SCHEMES):
        raise GuardConfigError(
            f"{WATCHER_ENV_VARS['rpc_url']} harus dimulai dengan salah satu dari: "
            f"{', '.join(RPC_URL_SCHEMES)}"
        )

    network = _load_network(from_file)
    expected_chain_id = expected_chain_id_for_network(network)
    chain_id = _require_bounded_int(
        WATCHER_ENV_VARS["chain_id"],
        value_of("chain_id"),
        default=expected_chain_id,
        minimum=1,
        maximum=_CHAIN_ID_MAX,
    )
    if chain_id != expected_chain_id:
        raise GuardConfigError(
            f"{WATCHER_ENV_VARS['chain_id']} harus {expected_chain_id} "
            f"untuk jaringan {network!r}, dapat {chain_id}. Satu jaringan punya "
            "tepat satu chain; lihat payment_guard.chains."
        )

    usdc_address = _require_address(
        WATCHER_ENV_VARS["usdc_address"], value_of("usdc_address") or DEFAULT_USDC_ADDRESS
    )

    raw_wallet = value_of("wallet_address")
    if not raw_wallet:
        raise GuardConfigError(
            f"{WATCHER_ENV_VARS['wallet_address']} belum diisi. Watcher menyaring log "
            "Transfer per pengirim (PRD v2 bagian 10), jadi ia harus tahu alamat wallet "
            "Executor. Isi alamatnya — bukan kuncinya; Watcher tidak pernah membaca "
            "berkas kunci."
        )
    wallet_address = _require_address(WATCHER_ENV_VARS["wallet_address"], raw_wallet)
    if wallet_address == usdc_address:
        raise GuardConfigError(
            f"{WATCHER_ENV_VARS['wallet_address']} sama dengan "
            f"{WATCHER_ENV_VARS['usdc_address']}; salah satu salah diisi"
        )

    confirmations = _require_bounded_int(
        WATCHER_ENV_VARS["confirmations"],
        value_of("confirmations"),
        default=DEFAULT_CONFIRMATIONS,
        minimum=CONFIRMATIONS_MIN,
        maximum=CONFIRMATIONS_MAX,
    )
    reorg_lookback = _require_bounded_int(
        WATCHER_ENV_VARS["reorg_lookback"],
        value_of("reorg_lookback"),
        default=DEFAULT_REORG_LOOKBACK,
        minimum=REORG_LOOKBACK_MIN,
        maximum=REORG_LOOKBACK_MAX,
    )
    if reorg_lookback <= confirmations:
        raise GuardConfigError(
            f"{WATCHER_ENV_VARS['reorg_lookback']} ({reorg_lookback}) harus lebih besar "
            f"dari {WATCHER_ENV_VARS['confirmations']} ({confirmations}). Jendela yang "
            "tidak melampaui kedalaman konfirmasi tidak pernah memuat baris final, jadi "
            "reorg yang lebih dalam dari kedalaman konfirmasi tidak akan pernah terlihat."
        )

    return WatcherConfig(
        db_url=db_url,
        rpc_url=rpc_url,
        chain_id=chain_id,
        usdc_address=usdc_address,
        wallet_address=wallet_address,
        confirmations=confirmations,
        scan_batch_size=_require_bounded_int(
            WATCHER_ENV_VARS["scan_batch_size"],
            value_of("scan_batch_size"),
            default=DEFAULT_SCAN_BATCH_SIZE,
            minimum=SCAN_BATCH_SIZE_MIN,
            maximum=SCAN_BATCH_SIZE_MAX,
        ),
        reorg_lookback=reorg_lookback,
        interval_seconds=_require_bounded_int(
            WATCHER_ENV_VARS["interval_seconds"],
            value_of("interval_seconds"),
            default=DEFAULT_WATCHER_INTERVAL_SECONDS,
            minimum=WATCHER_INTERVAL_SECONDS_MIN,
            maximum=WATCHER_INTERVAL_SECONDS_MAX,
        ),
        lock_timeout_ms=_require_bounded_int(
            WATCHER_ENV_VARS["lock_timeout_ms"],
            value_of("lock_timeout_ms"),
            default=DEFAULT_LOCK_TIMEOUT_MS,
            minimum=LOCK_TIMEOUT_MS_MIN,
            maximum=LOCK_TIMEOUT_MS_MAX,
        ),
    )


def load_reconciler_config(env_file: Path) -> ReconcilerConfig:
    """Muat dan validasi konfigurasi Reconciler.

    Raises:
        GuardConfigError: pada pelanggaran pertama yang ditemukan.
    """
    from_file = read_env_file(env_file)

    def value_of(field: str) -> str:
        var = RECONCILER_ENV_VARS[field]
        return (os.environ.get(var) or from_file.get(var) or "").strip()

    db_url = value_of("db_url")
    if not db_url:
        raise GuardConfigError(
            f"{RECONCILER_ENV_VARS['db_url']} belum diisi. Jalankan "
            f"`guard db bootstrap-roles` lebih dulu, atau isi di {env_file.name}."
        )

    network = _load_network(from_file)
    expected_chain_id = expected_chain_id_for_network(network)
    chain_id = _require_bounded_int(
        RECONCILER_ENV_VARS["chain_id"],
        value_of("chain_id"),
        default=expected_chain_id,
        minimum=1,
        maximum=_CHAIN_ID_MAX,
    )
    if chain_id != expected_chain_id:
        raise GuardConfigError(
            f"{RECONCILER_ENV_VARS['chain_id']} harus {expected_chain_id} "
            f"untuk jaringan {network!r}, dapat {chain_id}"
        )

    return ReconcilerConfig(
        db_url=db_url,
        chain_id=chain_id,
        deadline_seconds=_require_bounded_int(
            RECONCILER_ENV_VARS["deadline_seconds"],
            value_of("deadline_seconds"),
            default=DEFAULT_RECONCILE_DEADLINE_SECONDS,
            minimum=RECONCILE_DEADLINE_SECONDS_MIN,
            maximum=RECONCILE_DEADLINE_SECONDS_MAX,
        ),
        lock_timeout_ms=_require_bounded_int(
            RECONCILER_ENV_VARS["lock_timeout_ms"],
            value_of("lock_timeout_ms"),
            default=DEFAULT_LOCK_TIMEOUT_MS,
            minimum=LOCK_TIMEOUT_MS_MIN,
            maximum=LOCK_TIMEOUT_MS_MAX,
        ),
    )


def load_wallet_key_path(env_file: Path, *, repo_root: Path) -> Path:
    """Path berkas kunci wallet Executor saja, tanpa konfigurasi lain.

    Dipakai ``guard wallet``, yang hanya mencetak alamat turunan. Ia **tidak**
    memakai :func:`load_executor_config`, dan itu bukan penghematan: pemuat itu
    menuntut ``EXECUTOR_DB_URL`` dan memvalidasi ``RPC_URL``, jadi
    ``guard wallet`` akan gagal karena database yang belum di-bootstrap atau
    endpoint yang belum diisi — padahal ia tidak menyentuh keduanya. Perintah
    baca yang gagal karena hal yang tidak dipakainya akan mendorong orang
    memakai perintah lain yang "kebetulan" juga mencetak alamat, dan perintah
    itu menyiarkan transaksi.

    Pemeriksaannya sama dengan yang dilakukan pemuat Executor untuk field ini:
    berkasnya harus ada, dan harus di **luar** akar repo.

    Raises:
        GuardConfigError: bila variabelnya kosong, berkasnya tidak ada, atau
            berkasnya berada di dalam repo.
    """
    var = EXECUTOR_ENV_VARS["wallet_key_path"]
    from_file = read_env_file(env_file)
    raw = (os.environ.get(var) or from_file.get(var) or "").strip()
    if not raw:
        raise GuardConfigError(
            f"{var} belum diisi. Isi di {env_file.name} dengan path berkas kunci "
            "wallet Executor, di luar akar repo."
        )
    path = _require_existing_file(var, raw, env_file)
    _require_outside_repo(var, path, repo_root)
    return path


def _require_address(var: str, raw: str) -> str:
    """Alamat ternormalisasi lowercase, atau :class:`GuardConfigError`."""
    try:
        return normalize_address(raw)
    except AddressError as exc:
        raise GuardConfigError(f"{var} tidak sah: {exc}") from None


def _require_existing_file(var: str, raw: str, env_file: Path) -> Path:
    """Pastikan nilai ``var`` menunjuk berkas yang ada dan bisa dibaca.

    Menerima **nama variabel environment**, bukan nama field, supaya pemuat
    selain Guard (mis. Executor) bisa memakainya tanpa field-nya harus ada
    di :data:`GUARD_ENV_VARS`.
    """
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


def _require_outside_repo(var: str, path: Path, repo_root: Path) -> None:
    """Tolak bahan kunci yang berada di dalam akar repo.

    Berkas di dalam repo bisa ikut ter-commit karena satu ``git add -f``, satu
    aturan ``.gitignore`` yang berubah, atau satu salinan direktori. Menaruhnya
    di luar repo menghapus seluruh kelas kesalahan itu, bukan menguranginya.
    Security v1.1 I1 dan asumsi 2 di bagian 4.
    """
    root = repo_root.resolve()
    if path == root or root in path.parents:
        raise GuardConfigError(
            f"{var} berada di dalam akar repo: {path}\n"
            f"Akar repo: {root}\n"
            "Bahan kunci harus di luar repo supaya tidak bisa ter-commit karena "
            "kecelakaan. Pindahkan berkasnya, mis. ke direktori sendiri di luar "
            "pohon repo, lalu arahkan variabelnya ke sana."
        )


def _load_network(from_file: Mapping[str, str]) -> str:
    """Jaringan yang dipilih konfigurasi, dari environment lalu ``env_file``.

    Tidak disetel berarti produksi, jadi konfigurasi lama berperilaku sama
    persis seperti sebelum devnet ada. Nama yang tidak dikenal ditolak; lihat
    :func:`payment_guard.chains.normalize_network` untuk alasannya.

    Raises:
        GuardConfigError: bila nama jaringannya tidak dikenal.
    """
    raw = (os.environ.get(NETWORK_ENV_VAR) or from_file.get(NETWORK_ENV_VAR) or "").strip()
    try:
        return normalize_network(raw) if raw else NETWORK_PRODUCTION
    except NetworkError as exc:
        raise GuardConfigError(f"{NETWORK_ENV_VAR}: {exc}") from None


def _require_bounded_int(var: str, raw: str, *, default: int, minimum: int, maximum: int) -> int:
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise GuardConfigError(f"{var} harus bilangan bulat, dapat {raw!r}") from None
    if not minimum <= value <= maximum:
        raise GuardConfigError(f"{var} harus {minimum}..{maximum}, dapat {value}")
    return value
