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
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from psycopg import conninfo

from payment_guard.chains import DEVNET_LOCAL, NETWORK_DEVNET, NETWORK_ENV_VAR

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
    # ``str(v)`` bukan konversi, hanya penyempitan tipe. ``conninfo_to_dict``
    # mengembalikan ``dict[str, str | int]``, sedangkan parameter pertama
    # ``make_conninfo`` bernama ``conninfo`` dan bertipe ``str``, jadi
    # meneruskan dict itu dengan ``**`` ditolak ``mypy --strict``. Nilai yang
    # keluar dari URL selalu string; untuk nilai string ``str(v)`` adalah
    # identitas, jadi conninfo yang dihasilkan byte demi byte sama.
    return conninfo.make_conninfo(**{k: str(v) for k, v in bagian.items() if v is not None})


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

    Berkas yang sudah ada **tidak pernah ditimpa**, menyelaraskan fungsi ini
    dengan ``guard keygen``. Sebelumnya ia memanggil ``write_text`` langsung,
    yang memotong dan mengganti isi tanpa bertanya. Itu tidak berbahaya selama
    kuncinya diturunkan deterministik dari mnemonic Anvil indeks 1 — isinya
    selalu sama — tapi begitu indeks atau mnemonic-nya berubah, berkas kunci
    akan tergantikan dalam diam sementara kunci Guard di sebelahnya dilindungi
    penolakan.

    Tiga keadaan:

    * belum ada → ditulis
    * sudah ada, isinya **identik** → dibiarkan, dianggap sukses. Menulis ulang
      byte yang sama tidak menambah apa pun, dan tidak menimpa berarti satu hal
      lebih sedikit yang bisa salah.
    * sudah ada, isinya **berbeda** → **ditolak**. Keputusan menggantikan kunci
      milik operator, bukan milik skrip.

    Raises:
        DevnetError: bila path-nya di dalam akar repo, atau bila berkas yang
            sudah ada memuat kunci yang berbeda.
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

    isi = json.dumps({"address": akun.address, "private_key": akun.private_key}, indent=2) + "\n"

    if path.exists():
        try:
            lama = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise DevnetError(
                f"berkas kunci wallet {path} sudah ada tapi tidak bisa dibaca "
                f"({exc.strerror}), jadi tidak bisa dipastikan sama. Kunci lama "
                "tidak ditimpa."
            ) from None
        if lama == isi:
            return
        # Pesan ini TIDAK pernah mengutip isi berkas maupun kunci yang akan
        # ditulis: ia hanya menyebut path dan apa yang harus dilakukan
        # operator. Perbandingannya pun di memori, tidak dicetak.
        raise DevnetError(
            f"berkas kunci wallet {path} sudah ada dan memuat kunci yang "
            "BERBEDA dari yang akan ditulis. Kunci lama TIDAK ditimpa.\n"
            "Menggantikan kunci wallet adalah keputusan operator, bukan "
            "keputusan skrip ini: alamat yang berbeda berarti saldo, nonce, "
            "dan baris `executions` yang berbeda.\n"
            "Pindahkan berkas itu ke tempat aman lalu jalankan ulang bila "
            "penggantian memang dimaksudkan."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(isi, encoding="utf-8")
    # Hak akses dipersempit bila OS-nya mendukung. Di Windows ini no-op yang
    # tidak berbahaya; di POSIX ia menutup berkas dari pengguna lain.
    try:
        path.chmod(0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Token uji: deploy dan pendanaan
# --------------------------------------------------------------------------- #
#
# Deploy lewat web3 dari Python, **bukan** `forge create`. Alasannya jaringan
# container: dari dalam container Foundry, `127.0.0.1` adalah container itu
# sendiri, jadi `--rpc-url http://127.0.0.1:8545` menunjuk ke tempat yang
# salah, dan `--network host` tidak bisa diandalkan di Docker Desktop Windows.
# `forge build` tetap dipakai — ia berjalan offline dan menghasilkan artefak —
# tapi transaksi deploy-nya dikirim dari proses ini.

#: Artefak forge untuk kontrak token uji, relatif terhadap akar repo.
ARTEFAK_TOKEN: Final = Path("contracts") / "out" / "TestUSDC.sol" / "TestUSDC.json"

#: Desimal yang WAJIB dimiliki token devnet. Guard menolak start bila bukan 6.
DESIMAL_WAJIB: Final = 6


@dataclass(frozen=True)
class ArtefakToken:
    """ABI dan bytecode kontrak token uji."""

    abi: list[Any]
    bytecode: str


def load_token_artifact(path: Path) -> ArtefakToken:
    """Baca artefak ``forge build`` untuk ``TestUSDC``.

    Memeriksa bentuknya, bukan hanya membacanya: artefak yang ada tapi tidak
    memuat bytecode akan menghasilkan transaksi deploy kosong yang "berhasil"
    dan meninggalkan alamat tanpa kode di dalamnya.

    Raises:
        DevnetError: bila berkasnya tidak ada, bukan JSON, atau bentuknya
            tidak seperti artefak forge.
    """
    if not path.exists():
        raise DevnetError(
            f"artefak kontrak {path} tidak ada. Jalankan `forge build` lebih dulu "
            "(lihat docs/devnet.md); ia berjalan offline dengan solc yang dipin."
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DevnetError(f"artefak kontrak {path} tidak bisa dibaca: {exc}") from None

    abi = document.get("abi")
    if not isinstance(abi, list) or not abi:
        raise DevnetError(f"artefak {path} tidak memuat ABI")

    mentah = document.get("bytecode")
    objek = mentah.get("object") if isinstance(mentah, dict) else mentah
    if not isinstance(objek, str) or len(objek) < 4:
        raise DevnetError(f"artefak {path} tidak memuat bytecode")

    nama_fungsi = {e.get("name") for e in abi if e.get("type") == "function"}
    for wajib in ("mint", "transfer", "balanceOf", "decimals"):
        if wajib not in nama_fungsi:
            raise DevnetError(f"ABI di {path} tidak punya fungsi {wajib!r}")

    return ArtefakToken(abi=abi, bytecode=objek)


def _web3(rpc_url: str) -> Any:
    """Klien web3 untuk devnet. Skema dan host diperiksa lebih dulu."""
    require_devnet_rpc(rpc_url)
    from web3 import Web3

    return Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))


def deploy_test_usdc(rpc_url: str, deployer: AkunDevnet, artefak: ArtefakToken) -> str:
    """Deploy ``TestUSDC`` dari ``deployer``. Mengembalikan alamatnya, lowercase.

    Raises:
        DevnetError: bila transaksinya gagal, atau alamat hasilnya tidak
            memuat kode.
    """
    from eth_account import Account

    w3 = _web3(rpc_url)
    akun = Account.from_key(deployer.private_key)
    kontrak = w3.eth.contract(abi=artefak.abi, bytecode=artefak.bytecode)

    tx = kontrak.constructor().build_transaction(
        {
            "from": akun.address,
            "nonce": w3.eth.get_transaction_count(akun.address),
            "chainId": DEVNET_CHAIN_ID,
            "gas": 2_000_000,
            "gasPrice": w3.eth.gas_price,
        }
    )
    ditandatangani = akun.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(ditandatangani.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

    if receipt["status"] != 1:
        raise DevnetError(f"deploy TestUSDC gagal, status receipt {receipt['status']}")
    alamat = str(receipt["contractAddress"]).lower()

    # Alamat tanpa kode berarti deploy yang "berhasil" tapi kosong.
    if w3.eth.get_code(receipt["contractAddress"]) in (b"", b"0x"):
        raise DevnetError(f"alamat {alamat} tidak memuat kode setelah deploy")

    desimal = (
        w3.eth.contract(address=receipt["contractAddress"], abi=artefak.abi)
        .functions.decimals()
        .call()
    )
    if desimal != DESIMAL_WAJIB:
        raise DevnetError(
            f"token di {alamat} punya decimals() {desimal}, bukan {DESIMAL_WAJIB}. "
            "Guard menolak start dengan desimal lain."
        )
    return alamat


def fund_executor_eth(rpc_url: str, address: str, wei: int) -> None:
    """Setel saldo ETH wallet Executor lewat ``anvil_setBalance``.

    Memakai cheatcode, bukan transfer: devnet tidak perlu mensimulasikan dari
    mana ETH-nya datang, dan transfer akan menghabiskan nonce deployer yang
    lebih berguna untuk hal lain.
    """
    rpc(rpc_url, "anvil_setBalance", [address, hex(wei)])


def mint_tusdc(
    rpc_url: str,
    deployer: AkunDevnet,
    token_address: str,
    to: str,
    amount: int,
    abi: list[Any],
) -> str:
    """Cetak ``amount`` base unit tUSDC ke ``to``. Mengembalikan hash transaksi.

    ``mint`` hanya boleh owner, dan owner-nya deployer.

    Raises:
        DevnetError: bila transaksinya gagal.
    """
    from eth_account import Account
    from web3 import Web3

    w3 = _web3(rpc_url)
    akun = Account.from_key(deployer.private_key)
    kontrak = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=abi)

    tx = kontrak.functions.mint(Web3.to_checksum_address(to), amount).build_transaction(
        {
            "from": akun.address,
            "nonce": w3.eth.get_transaction_count(akun.address),
            "chainId": DEVNET_CHAIN_ID,
            "gas": 200_000,
            "gasPrice": w3.eth.gas_price,
        }
    )
    ditandatangani = akun.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(ditandatangani.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt["status"] != 1:
        raise DevnetError(f"mint tUSDC gagal, status receipt {receipt['status']}")
    return "0x" + bytes(tx_hash).hex()


def token_balance(rpc_url: str, token_address: str, holder: str, abi: list[Any]) -> int:
    """Saldo tUSDC ``holder``, dalam base unit."""
    from web3 import Web3

    w3 = _web3(rpc_url)
    kontrak = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=abi)
    return int(kontrak.functions.balanceOf(Web3.to_checksum_address(holder)).call())


# --------------------------------------------------------------------------- #
# Menulis nilai ke .env.devnet
# --------------------------------------------------------------------------- #


def apply_env_updates(text: str, updates: Mapping[str, str]) -> str:
    """Setel ``KEY=value`` di isi berkas env, pertahankan sisanya.

    Fungsi murni atas teks, bukan atas berkas, supaya bisa diuji tanpa menulis
    apa pun — dan supaya tidak ada tes yang perlu menyentuh berkas yang memuat
    kredensial.

    Baris yang kuncinya sudah ada **diganti di tempat**, menjaga urutan dan
    komentar. Kunci yang belum ada ditambahkan di akhir. Baris lain tidak
    disentuh: berkas ini memuat lima URL runtime berkredensial, dan menulis
    ulangnya dari nol berarti kehilangan apa pun yang tidak dikenali fungsi
    ini.
    """
    sisa = dict(updates)
    keluaran: list[str] = []
    for baris in text.splitlines():
        telanjang = baris.strip()
        if telanjang and not telanjang.startswith("#") and "=" in telanjang:
            kunci = telanjang.split("=", 1)[0].strip()
            if kunci in sisa:
                keluaran.append(f"{kunci}={sisa.pop(kunci)}")
                continue
        keluaran.append(baris)
    keluaran.extend(f"{kunci}={nilai}" for kunci, nilai in sisa.items())
    return "\n".join(keluaran) + "\n"


def watcher_and_reconciler_settings() -> dict[str, str]:
    """Setelan numerik Watcher dan Reconciler, dari konstanta bawaan produksi.

    **Kenapa ini ada.** ``.env.devnet`` dibuat ``bootstrap-roles``, yang hanya
    menulis lima URL runtime. Kelima setelan di bawah karena itu tidak pernah
    ada di berkas, dan Watcher serta Reconciler memakai bawaan ``config.py``
    tanpa ada yang menyadarinya. Templat ``.env.devnet.example`` memuat nilai
    lain lagi (20, 2, 60), sehingga berkas templat **menyesatkan**: ia
    menjanjikan setelan yang tidak pernah berlaku.

    Nilainya diambil dari konstanta ``DEFAULT_*``, bukan ditulis ulang sebagai
    angka, dan namanya dari ``WATCHER_ENV_VARS``/``RECONCILER_ENV_VARS``, bukan
    ditulis ulang sebagai string. Dua salinan angka yang sama akan menyimpang;
    satu sumber tidak bisa.

    Nilai **templat** (`REORG_LOOKBACK=20`, `WATCHER_INTERVAL_SECONDS=2`,
    `RECONCILE_DEADLINE_SECONDS=60`) sengaja **tidak** dipakai. Deadline 60
    detik bisa memunculkan ``executed_not_settled`` palsu di skenario jalur
    bahagia, sebelum Watcher sempat menaikkan settlement menjadi ``final``.
    """
    from payment_guard.config import (
        DEFAULT_CONFIRMATIONS,
        DEFAULT_RECONCILE_DEADLINE_SECONDS,
        DEFAULT_REORG_LOOKBACK,
        DEFAULT_SCAN_BATCH_SIZE,
        DEFAULT_WATCHER_INTERVAL_SECONDS,
        RECONCILER_ENV_VARS,
        WATCHER_ENV_VARS,
    )

    return {
        WATCHER_ENV_VARS["confirmations"]: str(DEFAULT_CONFIRMATIONS),
        WATCHER_ENV_VARS["reorg_lookback"]: str(DEFAULT_REORG_LOOKBACK),
        WATCHER_ENV_VARS["scan_batch_size"]: str(DEFAULT_SCAN_BATCH_SIZE),
        WATCHER_ENV_VARS["interval_seconds"]: str(DEFAULT_WATCHER_INTERVAL_SECONDS),
        RECONCILER_ENV_VARS["deadline_seconds"]: str(DEFAULT_RECONCILE_DEADLINE_SECONDS),
    }


def update_env_file(path: Path, updates: Mapping[str, str]) -> None:
    """Terapkan ``updates`` ke berkas env, tanpa pernah mencetak isinya.

    Raises:
        DevnetError: bila berkasnya tidak ada atau tidak bisa ditulis.
    """
    if not path.exists():
        raise DevnetError(
            f"{path} tidak ada. Jalankan bootstrap-roles lebih dulu; ia yang membuat berkas itu."
        )
    try:
        lama = path.read_text(encoding="utf-8")
        path.write_text(apply_env_updates(lama, updates), encoding="utf-8")
    except OSError as exc:
        raise DevnetError(f"{path} tidak bisa ditulis: {exc.strerror}") from None


# --------------------------------------------------------------------------- #
# Mandate devnet
# --------------------------------------------------------------------------- #
#
# Mandate **tidak ditandatangani**. Yang ada hanya `mandate_hash`: SHA-256 atas
# JSON kanonik dengan awalan domain `payment-guard/mandate/v1\n`, tanpa kunci
# sama sekali (`payment_guard.mandate.compute_mandate_hash`). Kunci tanda
# tangan Guard tetap berperan, tapi perannya berbeda: `start_guard` memuat dan
# memvalidasinya supaya Guard bisa menandatangani **envelope** sesudahnya, jadi
# kunci yang rusak membuat Guard menolak start dan mandate pun tidak tertulis.
#
# Penulisannya lewat `guard_service.start_guard`, jalur resmi yang sama dengan
# `guard serve`: satu transaksi dengan advisory lock `mandate:<agent_id>` yang
# menyisipkan ke `mandates`, meng-upsert `active_mandates`, dan menulis audit
# `mandate_loaded`. Bukan SQL tangan, dan bukan lewat subproses `guard serve` —
# server MCP itu proses panjang, dan devnet hanya butuh efek startnya.
#
# Skema mandate TIDAK punya field alamat token: alamat tUSDC hidup di
# `USDC_ADDRESS` di env, dipakai Executor dan Watcher. Kaitan mandate ke token
# hasil deploy ditegakkan sebagai **pemeriksaan**, bukan sebagai field.

#: Indeks akun Anvil yang menjadi penerima yang diizinkan di mandate devnet.
#:
#: Bukan 0 (deployer) dan bukan 1 (Executor): penerima yang sama dengan
#: pengirim akan membuat saldo tidak berubah setelah pembayaran, dan skenario
#: jalur bahagia tidak bisa membedakan "terkirim" dari "tidak terkirim".
INDEKS_PENERIMA: Final = 2

#: Identitas mandate devnet. Tetap, karena devnet selalu satu agent.
DEVNET_AGENT_ID: Final = "devnet-agent"
DEVNET_MANDATE_ID: Final = "devnet-m-001"


def devnet_mandate_document(*, recipient: str) -> dict[str, object]:
    """Dokumen mandate devnet, dibangun di runtime.

    **Bukan berkas statis yang ter-commit**: penerimanya diturunkan dari
    mnemonic Anvil saat skrip berjalan, jadi tidak ada alamat yang ditulis
    sebagai literal di repo.

    ``chain`` adalah ``devnet-local``, yang hanya sah di jaringan devnet dan
    memetakan ke chain id 31337. Mandate ini **ditolak** Guard produksi, dan
    itu memang pengamannya (lihat ``payment_guard.chains``).
    """
    return {
        "mandate_id": DEVNET_MANDATE_ID,
        "agent_id": DEVNET_AGENT_ID,
        "chain": DEVNET_LOCAL,
        "max_per_tx": "50",
        "max_per_day": "200",
        "human_approval_above": "25",
        "allowed_recipients": [recipient],
        "retry_cooldown_seconds": 600,
        "aggregation_window_seconds": 3600,
        "decision_ttl_seconds": 900,
        "human_wait_ttl_seconds": 3600,
    }


def mandate_hash_of(document: dict[str, object]) -> str:
    """Hash mandate lewat jalur yang **sama** dengan Guard.

    Mendelegasikan ke ``payment_guard.mandate``, bukan menghitung SHA-256
    sendiri: hash yang hampir benar — misalnya tanpa awalan domain — tidak akan
    pernah cocok dengan yang dipakai Guard, dan perbedaannya hanya terlihat
    jauh di hilir.
    """
    from payment_guard.mandate import compute_mandate_hash, load_mandate

    return compute_mandate_hash(load_mandate(document, network=NETWORK_DEVNET))


def write_devnet_mandate(path: Path, document: dict[str, object]) -> str:
    """Tulis berkas mandate devnet. Mengembalikan ``mandate_hash``-nya.

    Berbeda dari berkas kunci, mandate **boleh** ditimpa: ia bukan rahasia,
    isinya deterministik dari akun Anvil, dan mandate yang berganti memang
    hal yang Guard tangani lewat ``active_mandates``.

    Raises:
        DevnetError: bila dokumennya tidak sah sebagai mandate devnet.
    """
    from payment_guard.mandate import MandateError

    try:
        hash_mandate = mandate_hash_of(document)
    except (MandateError, ValueError) as exc:
        raise DevnetError(f"mandate devnet tidak sah: {exc}") from None

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return hash_mandate


def require_token_matches_env(env_file: Path, token_address: str) -> None:
    """Pastikan ``USDC_ADDRESS`` di env cocok dengan token hasil deploy.

    Inilah kaitan mandate devnet ke alamat tUSDC: bukan sebuah field di
    mandate — skema mandate tidak punya tempat untuk alamat token — melainkan
    pemeriksaan bahwa lingkungan yang akan dipakai Guard, Executor, dan Watcher
    menunjuk ke kontrak yang baru saja dideploy.

    Tanpa pemeriksaan ini, devnet bisa berjalan dengan mandate baru di atas
    ``USDC_ADDRESS`` basi — tepatnya keadaan yang terjadi setelah Docker
    restart pada 17 Nov 2026.

    Raises:
        DevnetError: bila tidak cocok, atau bila nilainya tidak ada.
    """
    from payment_guard.config import read_env_file

    nilai = (read_env_file(env_file).get("USDC_ADDRESS") or "").strip().lower()
    if not nilai:
        raise DevnetError(f"USDC_ADDRESS belum terisi di {env_file.name}")
    if nilai != token_address.lower():
        raise DevnetError(
            f"USDC_ADDRESS di {env_file.name} menunjuk ke {nilai}, bukan ke token "
            f"yang baru dideploy ({token_address.lower()}). Lingkungan basi."
        )


def install_devnet_mandate(env_file: Path, repo_root: Path) -> tuple[str, str]:
    """Tulis mandate ke database lewat ``start_guard``, lalu tutup sesinya.

    Jalur resminya, dan karena itu ia juga yang: mengambil advisory lock
    ``mandate:<agent_id>``, menyisipkan ke ``mandates``, meng-upsert
    ``active_mandates``, dan menulis audit ``mandate_loaded`` — semuanya dalam
    satu transaksi.

    Sesi Guard memegang lock sesi ``guard:<agent_id>`` di koneksi dedikasi,
    jadi ia **wajib** ditutup; kalau tidak, Approvals CLI akan melihat Guard
    masih hidup dan skenario berikutnya menilai keadaan yang salah.

    Returns:
        ``(mandate_hash, agent_id)``.

    Raises:
        DevnetError: bila Guard menolak start.
    """
    from payment_guard.config import load_guard_config
    from payment_guard.guard_service import GuardStartupError, start_guard

    try:
        config = load_guard_config(env_file, repo_root=repo_root)
    except Exception as exc:
        raise DevnetError(
            f"konfigurasi Guard devnet ditolak: {type(exc).__name__}: {exc}"
        ) from None

    try:
        session = start_guard(config)
    except GuardStartupError as exc:
        raise DevnetError(f"Guard devnet menolak start: {exc}") from None

    try:
        return session.mandate_hash, session.agent_id
    finally:
        # Lock sesi dilepas di sini. Tanpa ini, `guard approvals` akan
        # menyimpulkan Guard masih hidup.
        session.close()


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
        # Dicatat SEBELUM menulis: `write_executor_wallet` membiarkan berkas
        # yang isinya sudah identik, jadi "ditulis" dan "dipakai ulang" adalah
        # dua hal berbeda dan laporannya tidak boleh menyamakan keduanya.
        sudah_ada = wallet_key.exists()
        write_executor_wallet(wallet_key, executor)
        print(f"wallet Executor devnet: {executor.address}")
        if sudah_ada:
            print(f"  kunci sudah ada dan identik, dibiarkan: {wallet_key}")
        else:
            print(f"  kunci ditulis ke {wallet_key} (di luar repo; kuncinya tidak dicetak)")
    except DevnetError as exc:
        print(f"gagal: {exc}", file=sys.stderr)
        return 1

    try:
        artefak = load_token_artifact(akar / ARTEFAK_TOKEN)
        deployer = akun_anvil(INDEKS_DEPLOYER)
        print(f"deploy TestUSDC dari {deployer.address} (akun Anvil 0)")
        token = deploy_test_usdc(args.rpc_url, deployer, artefak)
        print(f"  token uji (BUKAN USDC) di {token}, decimals() = {DESIMAL_WAJIB}")

        fund_executor_eth(args.rpc_url, executor.address, ETH_UNTUK_EXECUTOR_WEI)
        print(f"  ETH untuk Executor: {ETH_UNTUK_EXECUTOR_WEI / 10**18:.0f} ETH")

        tx = mint_tusdc(
            args.rpc_url, deployer, token, executor.address, TUSDC_UNTUK_EXECUTOR, artefak.abi
        )
        saldo = token_balance(args.rpc_url, token, executor.address, artefak.abi)
        print(f"  mint tUSDC: tx {tx}")
        print(f"  saldo Executor: {saldo} base unit ({saldo / 10**DESIMAL_WAJIB:.6f} tUSDC)")

        update_env_file(
            env_file,
            {
                "USDC_ADDRESS": token,
                "EXECUTOR_WALLET_ADDRESS": executor.address,
                "EXECUTOR_KEY_PATH": str(wallet_key),
                "GUARD_SIGNING_KEY_PATH": str(signing_key),
                "GUARD_PUBLIC_KEYS_PATH": str(public_keys),
                "GUARD_MANDATE_PATH": str(rahasia / "mandate_devnet.json"),
                "RPC_URL": args.rpc_url,
                "CHAIN_ID": str(DEVNET_CHAIN_ID),
                NETWORK_ENV_VAR: NETWORK_DEVNET,
                # Setelan Watcher dan Reconciler, eksplisit. Tanpa ini mereka
                # mewarisi bawaan config.py secara kebetulan, dan devnet
                # berubah tanpa suara begitu bawaan itu berubah.
                **watcher_and_reconciler_settings(),
            },
        )
        print(f"  {env_file.name} diperbarui (nilainya tidak dicetak)")

        # Kaitan mandate ke token hasil deploy: pemeriksaan, bukan field.
        # Skema mandate tidak punya tempat untuk alamat token.
        require_token_matches_env(env_file, token)

        mandate_path = rahasia / "mandate_devnet.json"
        penerima = akun_anvil(INDEKS_PENERIMA)
        dokumen = devnet_mandate_document(recipient=penerima.address)
        hash_berkas = write_devnet_mandate(mandate_path, dokumen)
        print(f"mandate devnet: chain {DEVNET_LOCAL} (chain id {DEVNET_CHAIN_ID})")
        print(f"  penerima diizinkan      : {penerima.address} (akun Anvil {INDEKS_PENERIMA})")
        print(f"  berkas                  : {mandate_path}")
        print(f"  mandate_hash dari berkas: {hash_berkas}")

        hash_db, agent_id = install_devnet_mandate(env_file, akar)
        print(f"  ditulis lewat start_guard, agent_id {agent_id}")
        print(f"  mandate_hash di database: {hash_db}")
        if hash_db != hash_berkas:
            raise DevnetError(
                f"mandate_hash berkas ({hash_berkas}) berbeda dari yang dimuat "
                f"Guard ({hash_db}). Jalur hash skrip dan jalur Guard menyimpang."
            )
    except DevnetError as exc:
        print(f"gagal: {exc}", file=sys.stderr)
        return 1

    print("\ndevnet lokal siap. Skenario (a)-(e) belum dijalankan.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
