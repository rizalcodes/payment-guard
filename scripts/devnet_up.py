"""Bangun devnet lokal Payment Guard dari nol, satu perintah.

    uv run --no-sync python scripts/devnet_up.py

**Ini devnet lokal, bukan testnet.** Chain id 31337 di ``127.0.0.1:8545``,
database ``payment_guard_devnet`` di ``127.0.0.1:5436``. Lihat
``docs/devnet.md``.

Tiga pengaman, dan ketiganya **menolak jalan** alih-alih memperingatkan:

1. **Port 5434 dan 5435 ditolak by name.** Yang pertama cluster operator, yang
   kedua cluster tes. Skrip ini membuat dan menghapus role dan database, dan
   role Postgres bersifat cluster-wide.
2. **Chain id wajib 31337** dan RPC wajib loopback. Satu salah ketik di
   ``RPC_URL`` tidak boleh bisa membuat skrip ini mendanai wallet di jaringan
   sungguhan.
3. **Database devnet yang sudah berisi tabel ditolak**, kecuali ``--recreate``.
   Alasannya ada di ``docs/devnet.md``: state Anvil hilang saat container
   restart sedangkan volume Postgres bertahan, jadi "idempoten" berarti
   memakai database lama di atas chain baru yang kosong — kursor menunjuk blok
   yang tidak ada, dan rekonsiliasi menghasilkan angka yang tidak berarti.

Kunci wallet Executor **diturunkan dari mnemonic bawaan Anvil saat runtime**.
Tidak ada literal heksa kunci di repo ini: literal seperti itu memicu gitleaks,
dan kunci yang ditulis di berkas yang di-commit adalah kunci yang bocor.
Mnemonic-nya sendiri publik — itulah sebabnya ``load_wallet`` menolak akun
Anvil di luar jaringan devnet.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from psycopg import conninfo

#: Chain id devnet. Bawaan Anvil, tidak pernah muncul di jaringan publik.
DEVNET_CHAIN_ID: Final = 31337

#: Nama database devnet. Wajib persis ini.
DEVNET_DBNAME: Final = "payment_guard_devnet"

#: Port cluster devnet.
DEVNET_PORT: Final = "5436"

#: Port yang **ditolak by name**, dengan alasannya masing-masing.
PORT_TERLARANG: Final[dict[str, str]] = {
    "5434": "cluster operator; skrip ini membuat dan menghapus role, dan role "
    "Postgres bersifat cluster-wide",
    "5435": "cluster tes integrasi; suite tes memilikinya",
}

#: Host yang diterima untuk RPC maupun database.
LOOPBACK: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})

#: Skema RPC yang diterima. Hanya HTTP lokal.
SKEMA_RPC: Final[tuple[str, ...]] = ("http://", "https://")

#: Mnemonic bawaan Anvil. PUBLIK, dan itu justru intinya.
ANVIL_MNEMONIC: Final = "test test test test test test test test test test test junk"

#: Jalur derivasi BIP-44 Ethereum.
JALUR_AKUN: Final = "m/44'/60'/0'/0/{indeks}"

#: Indeks akun Anvil yang dipakai.
#:
#: 0 tetap deployer dan pendana; Executor memakai 1. Memisahkannya membuat
#: saldo Executor hanya berubah karena pembayaran, bukan karena ongkos deploy.
INDEKS_DEPLOYER: Final = 0
INDEKS_EXECUTOR: Final = 1

#: ETH yang diberikan ke wallet Executor, dalam wei. 10 ETH.
ETH_UNTUK_EXECUTOR_WEI: Final = 10 * 10**18

#: tUSDC yang dicetak ke wallet Executor, dalam base unit (6 desimal). 500.
TUSDC_UNTUK_EXECUTOR: Final = 500 * 10**6


class DevnetError(RuntimeError):
    """Devnet tidak bisa dibangun. Pesannya tidak pernah memuat password."""


# --------------------------------------------------------------------------- #
# Pengaman praflight. Fungsi murni: tidak menyentuh jaringan maupun database.
# --------------------------------------------------------------------------- #


def require_devnet_rpc(url: str) -> None:
    """Tolak URL RPC yang bukan loopback.

    Raises:
        DevnetError: bila skema atau hostnya bukan yang diizinkan.
    """
    if not url.startswith(SKEMA_RPC):
        raise DevnetError(
            f"RPC_URL harus dimulai dengan salah satu dari {', '.join(SKEMA_RPC)}, dapat {url!r}"
        )
    # ``urlsplit`` dipakai, bukan ``split(":")`` tangan: host IPv6 ditulis
    # dalam tanda kurung (``http://[::1]:8545``), dan pemisahan naif memotong
    # ``::1`` menjadi string kosong lalu menolak host yang sebenarnya sah.
    host = urllib.parse.urlsplit(url).hostname or ""
    if host not in LOOPBACK:
        raise DevnetError(
            f"host RPC {host!r} bukan loopback. Devnet hanya pernah berjalan di "
            f"127.0.0.1; menunjuk ke host lain berarti skrip ini bisa mendanai "
            f"wallet di jaringan sungguhan."
        )


def require_devnet_chain_id(seen: object) -> None:
    """Tolak chain id apa pun selain 31337.

    Raises:
        DevnetError: bila bukan 31337, atau bukan integer.
    """
    if not isinstance(seen, int) or isinstance(seen, bool):
        raise DevnetError(f"chain id dari RPC bukan integer: {seen!r}")
    if seen != DEVNET_CHAIN_ID:
        raise DevnetError(
            f"chain id dari RPC adalah {seen}, bukan {DEVNET_CHAIN_ID}. "
            f"Node di ujung RPC_URL bukan devnet lokal."
        )


def require_devnet_admin_url(url: str) -> None:
    """Tolak URL admin yang bukan cluster devnet.

    Urutannya disengaja: port terlarang diperiksa **lebih dulu**, supaya
    pesannya menyebut cluster mana yang hampir disentuh alih-alih mengeluh
    soal nama database.

    Raises:
        DevnetError: bila port, host, atau nama databasenya salah.
    """
    bagian = conninfo.conninfo_to_dict(url)
    port = str(bagian.get("port") or "")
    host = str(bagian.get("host") or "")
    dbname = str(bagian.get("dbname") or "")

    if port in PORT_TERLARANG:
        raise DevnetError(
            f"port {port} adalah {PORT_TERLARANG[port]}. Devnet memakai {DEVNET_PORT}."
        )
    if host not in LOOPBACK:
        raise DevnetError(f"host database {host!r} bukan loopback")
    if port != DEVNET_PORT:
        raise DevnetError(f"port database {port!r} bukan {DEVNET_PORT} (cluster devnet)")
    if dbname != DEVNET_DBNAME:
        raise DevnetError(f"nama database {dbname!r} bukan {DEVNET_DBNAME!r}")


def require_empty_devnet_db(tables: list[str], *, recreate: bool) -> None:
    """Tolak database devnet yang sudah berisi tabel.

    Args:
        tables: nama tabel yang ada di schema ``public``.
        recreate: bila True, state lama memang diminta dihapus.

    Raises:
        DevnetError: bila ada tabel dan ``recreate`` False.
    """
    if not tables or recreate:
        return
    raise DevnetError(
        f"database {DEVNET_DBNAME} sudah berisi {len(tables)} tabel "
        f"({', '.join(sorted(tables)[:5])}{'...' if len(tables) > 5 else ''}).\n"
        "Devnet tidak dipakai ulang: state Anvil hilang saat container restart "
        "sedangkan volume Postgres bertahan, jadi database lama di atas chain "
        "baru menghasilkan kursor yang menunjuk blok yang tidak ada.\n"
        "Jalankan ulang dengan --recreate bila state lama memang boleh dihapus."
    )


# --------------------------------------------------------------------------- #
# RPC. urllib, bukan curl: curl tidak bisa menjangkau loopback di mesin ini.
# --------------------------------------------------------------------------- #


def rpc(url: str, method: str, params: list[Any] | None = None, *, timeout: int = 30) -> Any:
    """Satu panggilan JSON-RPC. Mengembalikan ``result``.

    Raises:
        DevnetError: bila RPC menjawab error atau tidak bisa dijangkau.
    """
    # Skema diperiksa lagi di sini, bukan hanya di praflight. ``rpc`` bisa
    # dipanggil dari tempat lain kelak, dan ``urlopen`` menerima ``file://``
    # bila dibiarkan — itu yang dikeluhkan ruff S310. Pemeriksaan ini membuat
    # penekanan di bawah jujur, bukan sekadar membungkam pemindai.
    require_devnet_rpc(url)

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []})
    request = urllib.request.Request(  # noqa: S310 - skema dijaga require_devnet_rpc di atas
        url, data=body.encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            document = json.loads(response.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise DevnetError(f"RPC {method} gagal: {exc}") from None
    if "error" in document:
        raise DevnetError(f"RPC {method} ditolak node: {document['error']}")
    return document.get("result")


def chain_id_of(url: str) -> int:
    """Chain id node, sebagai integer."""
    raw = rpc(url, "eth_chainId")
    try:
        return int(str(raw), 16)
    except (TypeError, ValueError):
        raise DevnetError(f"eth_chainId mengembalikan nilai yang tidak terbaca: {raw!r}") from None


# --------------------------------------------------------------------------- #
# Kunci devnet, diturunkan saat runtime
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AkunDevnet:
    """Satu akun Anvil. ``private_key`` tidak pernah dicetak."""

    address: str
    private_key: str


def akun_anvil(indeks: int) -> AkunDevnet:
    """Akun Anvil ke-``indeks``, diturunkan dari mnemonic bawaannya.

    Diturunkan, bukan ditulis sebagai literal: literal kunci privat memicu
    gitleaks dan menggagalkan pre-commit, dan kunci yang ter-commit adalah
    kunci yang bocor. Mnemonic-nya publik.
    """
    from eth_account import Account

    Account.enable_unaudited_hdwallet_features()
    akun = Account.from_mnemonic(ANVIL_MNEMONIC, account_path=JALUR_AKUN.format(indeks=indeks))
    kunci = akun.key.hex()
    return AkunDevnet(
        address=str(akun.address).lower(),
        private_key=kunci if kunci.startswith("0x") else "0x" + kunci,
    )


# --------------------------------------------------------------------------- #
# Bootstrap dan migrasi
# --------------------------------------------------------------------------- #
#
# Keduanya dijalankan lewat perintah `guard` yang sudah ada, bukan SQL tangan.
# Alasannya bukan kerapian: `bootstrap-roles` yang menulis role dan kelima URL
# runtime, dan `db migrate` yang menjaga `schema_migrations`. SQL tangan akan
# menghasilkan devnet yang skemanya mirip tapi riwayat migrasinya tidak pernah
# ada, dan itu devnet yang menguji hal lain.

#: Nama berkas env runtime devnet, relatif terhadap akar repo.
DEVNET_ENV_FILENAME: Final = ".env.devnet"

#: Nama berkas URL migrator devnet. Terpisah dari env runtime, seperti di
#: produksi: URL pemilik objek tidak pernah ikut di berkas yang dibaca lima
#: proses runtime.
DEVNET_MIGRATOR_ENV_FILENAME: Final = ".env.devnet.migrator"

#: Database pemeliharaan untuk DROP/CREATE. Tidak bisa menghapus database yang
#: sedang dipakai koneksinya sendiri.
MAINTENANCE_DBNAME: Final = "postgres"


def maintenance_url_for(admin_url: str) -> str:
    """URL ke database pemeliharaan di cluster yang sama.

    ``DROP DATABASE`` tidak bisa dijalankan dari koneksi ke database yang akan
    dihapus, jadi ``--recreate`` butuh pintu masuk lain di cluster yang sama.

    Pengamannya diperiksa **lagi** di sini, bukan dipercaya dari pemanggil:
    fungsi ini menghasilkan URL yang nama databasenya **bukan**
    ``payment_guard_devnet``, jadi ia melewati satu-satunya pengaman yang
    menyebut nama itu. Yang tersisa yang masih menjaga adalah port dan host.

    Raises:
        DevnetError: bila port atau hostnya bukan cluster devnet.
    """
    bagian = conninfo.conninfo_to_dict(admin_url)
    port = str(bagian.get("port") or "")
    host = str(bagian.get("host") or "")
    if port in PORT_TERLARANG:
        raise DevnetError(
            f"port {port} adalah {PORT_TERLARANG[port]}. Devnet memakai {DEVNET_PORT}."
        )
    if port != DEVNET_PORT:
        raise DevnetError(f"port database {port!r} bukan {DEVNET_PORT} (cluster devnet)")
    if host not in LOOPBACK:
        raise DevnetError(f"host database {host!r} bukan loopback")
    bagian["dbname"] = MAINTENANCE_DBNAME
    return conninfo.make_conninfo(**{k: v for k, v in bagian.items() if v is not None})


def bootstrap_command(env_file: Path, migrator_env_file: Path) -> list[str]:
    """Argumen ``guard db bootstrap-roles`` untuk devnet.

    Flagnya dari ``--help``, bukan ingatan: ``--env-file``,
    ``--migrator-env-file``, ``--no-prompt``. ``--allow-remote-admin``
    **tidak** dipakai — URL admin devnet selalu loopback, dan membiarkan flag
    itu lewat berarti pengaman host hilang tanpa ada yang memintanya.

    Fungsi ini murni supaya satu salah ketik flag tertangkap tes, bukan saat
    skrip berjalan setengah jalan.
    """
    return [
        sys.executable,
        "-m",
        "payment_guard.cli",
        "db",
        "bootstrap-roles",
        "--env-file",
        str(env_file),
        "--migrator-env-file",
        str(migrator_env_file),
        "--no-prompt",
    ]


def migrate_command(migrator_env_file: Path, migrations_dir: Path | None = None) -> list[str]:
    """Argumen ``guard db migrate`` untuk devnet.

    ``--migrations-dir`` hanya ikut bila diberikan; bawaannya ``migrations/``
    di akar repo, dan devnet memakai migrasi yang sama dengan produksi — kalau
    tidak, ia menguji skema yang bukan skema produk.
    """
    argv = [
        sys.executable,
        "-m",
        "payment_guard.cli",
        "db",
        "migrate",
        "--migrator-env-file",
        str(migrator_env_file),
    ]
    if migrations_dir is not None:
        argv += ["--migrations-dir", str(migrations_dir)]
    return argv


def devnet_tables(admin_url: str) -> list[str]:
    """Nama tabel di schema ``public`` database devnet.

    Dipakai :func:`require_empty_devnet_db` untuk memutuskan menolak atau
    jalan. Hanya SELECT.
    """
    import psycopg

    require_devnet_admin_url(admin_url)
    with psycopg.connect(admin_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        return sorted(str(baris[0]) for baris in cur.fetchall())


def recreate_devnet_database(admin_url: str) -> None:
    """Hapus lalu buat ulang database devnet. Hanya dipanggil dengan ``--recreate``.

    Raises:
        DevnetError: bila clusternya bukan devnet, atau DROP/CREATE gagal.
    """
    import psycopg
    from psycopg import sql

    require_devnet_admin_url(admin_url)
    pemeliharaan = maintenance_url_for(admin_url)
    try:
        with psycopg.connect(pemeliharaan, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(DEVNET_DBNAME)
                )
            )
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DEVNET_DBNAME)))
    except Exception as exc:
        raise DevnetError(f"gagal membangun ulang database devnet: {type(exc).__name__}") from None


# --------------------------------------------------------------------------- #
# Kunci: Guard signing, dan wallet Executor
# --------------------------------------------------------------------------- #
#
# Ketiga berkas kunci WAJIB di luar akar repo. Itu bukan preferensi: config.py
# menolaknya lewat `_require_outside_repo` di tiga tempat — kunci tanda tangan
# Guard (baris 309), kunci tanda tangan Approvals (407), dan kunci wallet
# Executor (537).
#
# Perlu dicatat karena membingungkan: **bawaan `guard keygen` justru di dalam
# repo** (`keys/guard_signing.json`), dan path itu akan ditolak Guard saat
# start. Devnet karena itu selalu memberi path eksplisit.

#: Nama direktori rahasia devnet, **di samping** akar repo.
#:
#: Di luar repo, jadi ia tidak bisa ter-commit bahkan karena kecelakaan — lebih
#: kuat daripada mengandalkan `.gitignore`, yang hanya menahan berkas yang
#: secara teknis masih bisa di-`git add -f`.
DEVNET_KEY_DIRNAME: Final = "payment-guard-devnet-rahasia"


def devnet_key_dir(repo_root: Path) -> Path:
    """Direktori rahasia devnet, di samping akar repo.

    Bukan di dalam repo: ``config.py`` menolak kunci di dalam akar repo, dan
    di luar repo berarti git tidak pernah melihatnya sama sekali.
    """
    return repo_root.parent / DEVNET_KEY_DIRNAME


def keygen_command(signing_key_file: Path, public_keys_file: Path) -> list[str]:
    """Argumen ``guard keygen`` untuk devnet.

    Flagnya dari ``--help``: ``--signing-key-file`` dan ``--public-keys-file``.
    Keduanya **selalu** diberikan eksplisit — bawaan keygen menaruh kunci
    rahasia di ``keys/`` di dalam repo, dan Guard akan menolak path itu saat
    start.
    """
    return [
        sys.executable,
        "-m",
        "payment_guard.cli",
        "keygen",
        "--signing-key-file",
        str(signing_key_file),
        "--public-keys-file",
        str(public_keys_file),
    ]


def keygen_decision(signing_key_file: Path, public_keys_file: Path) -> str:
    """Apakah keygen perlu dijalankan: ``"buat"``, ``"pakai-ulang"``, atau tolak.

    ``guard keygen`` **menolak menimpa** kunci tanda tangan yang sudah ada, dan
    penolakan itu benar: menimpanya menghilangkan kunci yang mungkin masih
    dipakai envelope yang beredar, dan itu tidak bisa dibatalkan (Security v1.1
    runbook 8.1). Jadi ``--recreate`` tidak boleh mengakalinya dengan menghapus
    berkas kunci — ia harus memutuskan dengan sadar.

    * belum ada keduanya → ``"buat"``
    * ada keduanya → ``"pakai-ulang"``. Database devnet memang dihapus, tapi
      kunci tanda tangan tidak perlu berganti: envelope dari devnet lama tidak
      berlaku di database yang baru, jadi memakai ulang kuncinya tidak
      menyembunyikan apa pun.
    * kunci rahasia ada tapi registri publiknya tidak → **ditolak**. Keadaan
      itu tidak bisa diperbaiki skrip: keygen tidak akan menimpa kuncinya, dan
      menulis registri baru dari kunci lama bukan pekerjaannya. Operator yang
      memindahkan kuncinya, sesuai prosedur rotasi.

    Raises:
        DevnetError: pada keadaan yang tidak bisa diperbaiki skrip.
    """
    ada_rahasia = signing_key_file.exists()
    ada_publik = public_keys_file.exists()
    if ada_rahasia and ada_publik:
        return "pakai-ulang"
    if ada_rahasia and not ada_publik:
        raise DevnetError(
            f"kunci tanda tangan {signing_key_file} ada, tapi registri publik "
            f"{public_keys_file} tidak.\n"
            "keygen tidak akan menimpa kunci yang sudah ada, dan skrip ini "
            "tidak menulis registri dari kunci lama.\n"
            "Pindahkan berkas kunci itu ke tempat aman lalu jalankan ulang "
            "(prosedur rotasi, Security v1.1 runbook 8.1)."
        )
    return "buat"


def write_executor_wallet(path: Path, akun: AkunDevnet) -> None:
    """Tulis berkas kunci wallet Executor devnet.

    Bentuknya yang dituntut :func:`payment_guard.chain.load_wallet`: tepat dua
    field, ``address`` dan ``private_key``, dan alamatnya wajib cocok dengan
    kunci yang diturunkannya.

    Berkas ini memuat kunci **publik-diketahui** (akun Anvil), dan itu sebabnya
    ``load_wallet`` menolaknya di luar jaringan devnet.

    Raises:
        DevnetError: bila path-nya berada di dalam akar repo.
    """
    akar = Path(__file__).resolve().parent.parent
    try:
        path.resolve().relative_to(akar)
    except ValueError:
        pass  # Di luar repo: inilah yang benar.
    else:
        raise DevnetError(
            f"berkas kunci wallet tidak boleh di dalam akar repo ({path}). "
            "config.py menolaknya, dan kunci di dalam repo adalah kunci yang "
            "bisa ter-commit."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"address": akun.address, "private_key": akun.private_key}, indent=2) + "\n",
        encoding="utf-8",
    )
    # Hak akses dipersempit bila OS-nya mendukung. Di Windows ini no-op yang
    # tidak berbahaya; di POSIX ia menutup berkas dari pengguna lain.
    try:
        path.chmod(0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Langkah-langkah
# --------------------------------------------------------------------------- #


def jalankan(perintah: list[str], *, env: dict[str, str] | None = None, label: str) -> str:
    """Jalankan subproses dan kembalikan stdout-nya.

    Raises:
        DevnetError: bila keluar dengan kode bukan nol.
    """
    lingkungan = {**os.environ, **(env or {})}
    hasil = subprocess.run(  # noqa: S603 - perintah dibangun di modul ini, bukan dari input
        perintah, capture_output=True, text=True, env=lingkungan, check=False
    )
    if hasil.returncode != 0:
        raise DevnetError(
            f"{label} gagal (kode {hasil.returncode}).\n"
            f"stdout: {hasil.stdout.strip()[:800]}\n"
            f"stderr: {hasil.stderr.strip()[:800]}"
        )
    return hasil.stdout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/devnet_up.py",
        description="Bangun devnet lokal Payment Guard dari nol (chain id 31337).",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help=(
            "hapus database devnet yang sudah ada lebih dulu. Tanpa ini, "
            "database yang sudah berisi tabel membuat skrip menolak jalan."
        ),
    )
    parser.add_argument(
        "--rpc-url",
        default="http://127.0.0.1:8545",
        help="RPC devnet (default: http://127.0.0.1:8545). Wajib loopback.",
    )
    parser.add_argument(
        "--admin-db-url",
        default=None,
        help=(
            "URL admin cluster devnet. Bawaannya dibaca dari "
            "PAYMENT_GUARD_DEVNET_ADMIN_DB_URL. Wajib port 5436 dan database "
            f"{DEVNET_DBNAME}."
        ),
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="jalankan pengaman praflight lalu berhenti; tidak mengubah apa pun",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    admin_url = args.admin_db_url or os.environ.get("PAYMENT_GUARD_DEVNET_ADMIN_DB_URL", "")
    if not admin_url:
        print(
            "PAYMENT_GUARD_DEVNET_ADMIN_DB_URL belum disetel, dan --admin-db-url "
            "tidak diberikan. Devnet tidak pernah menebak URL database.",
            file=sys.stderr,
        )
        return 2

    try:
        require_devnet_rpc(args.rpc_url)
        require_devnet_admin_url(admin_url)
        print(f"praflight: RPC {args.rpc_url} loopback, URL admin cluster devnet {DEVNET_PORT}")
        require_devnet_chain_id(chain_id_of(args.rpc_url))
        print(f"praflight: chain id {DEVNET_CHAIN_ID} terkonfirmasi dari RPC")
    except DevnetError as exc:
        print(f"praflight gagal: {exc}", file=sys.stderr)
        return 2

    if args.preflight_only:
        print("praflight-only: berhenti di sini, tidak ada yang diubah.")
        return 0

    akar = Path(__file__).resolve().parent.parent
    env_file = akar / DEVNET_ENV_FILENAME
    migrator_env = akar / DEVNET_MIGRATOR_ENV_FILENAME

    try:
        if args.recreate:
            print("--recreate: menghapus dan membangun ulang database devnet")
            recreate_devnet_database(admin_url)
        require_empty_devnet_db(devnet_tables(admin_url), recreate=args.recreate)
        print("praflight: database devnet kosong")

        # URL admin masuk lewat environment SEMENTARA dan tidak pernah
        # dicetak, sama seperti prosedur produksi di docs/runbook.md.
        print(f"bootstrap-roles -> {env_file.name}, {migrator_env.name}")
        keluaran = jalankan(
            bootstrap_command(env_file, migrator_env),
            env={"PAYMENT_GUARD_ADMIN_DB_URL": admin_url},
            label="guard db bootstrap-roles",
        )
        for baris in keluaran.splitlines():
            if baris.strip():
                print(f"  {baris}")

        print("db migrate")
        keluaran = jalankan(
            migrate_command(migrator_env),
            label="guard db migrate",
        )
        for baris in keluaran.splitlines():
            if baris.strip():
                print(f"  {baris}")
    except DevnetError as exc:
        print(f"gagal: {exc}", file=sys.stderr)
        return 1

    rahasia = devnet_key_dir(akar)
    signing_key = rahasia / "guard_signing_devnet.json"
    # Registri kunci publik juga di luar repo, meski ia bukan rahasia.
    # Alasannya bukan kerahasiaan melainkan kebersihan: ia berubah setiap
    # devnet dibangun ulang, dan di akar repo ia muncul sebagai berkas
    # untracked yang mencemari `git status` dan bisa ter-commit karena
    # kecelakaan. Revisi pertama menaruhnya di akar repo dan langsung
    # menghasilkan tepat itu.
    public_keys = rahasia / "guard_public_devnet.json"
    wallet_key = rahasia / "executor_wallet_devnet.json"

    try:
        putusan = keygen_decision(signing_key, public_keys)
        if putusan == "pakai-ulang":
            print(
                f"keygen dilewati: {signing_key.name} dan {public_keys.name} "
                "sudah ada, dipakai ulang. keygen tidak pernah menimpa kunci."
            )
        else:
            print(f"keygen -> {signing_key.name} (di luar repo), {public_keys.name}")
            keluaran = jalankan(keygen_command(signing_key, public_keys), label="guard keygen")
            for baris in keluaran.splitlines():
                if baris.strip():
                    print(f"  {baris}")

        executor = akun_anvil(INDEKS_EXECUTOR)
        write_executor_wallet(wallet_key, executor)
        print(f"wallet Executor devnet: {executor.address}")
        print(f"  kunci ditulis ke {wallet_key} (di luar repo; kuncinya tidak dicetak)")
    except DevnetError as exc:
        print(f"gagal: {exc}", file=sys.stderr)
        return 1

    print(
        "\nLangkah berikutnya (deploy token uji, pendanaan, .env.devnet "
        "lengkap, mandate devnet) belum diimplementasi di revisi ini."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
