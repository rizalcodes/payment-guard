"""Entry point ``guard``.

Perintah yang tersedia sampai M5:

    guard db migrate          terapkan migrasi yang belum terapan (role migrator)
    guard db status           laporkan migrasi terapan dan tertunda
    guard db bootstrap-roles  buat role database (admin, sekali)
    guard keygen              buat pasangan kunci Ed25519 (sekali)
    guard serve               jalankan server MCP stdio (dua tool)
    guard approvals ...       setujui atau tolak keputusan needs_human
    guard execute             eksekusi satu keputusan approved ke chain
    guard recover             selesaikan execution yang tertinggal
    guard verify              periksa envelope dengan kunci publik saja

``migrate`` dan ``status`` hanya membaca ``.env.migrator``; keduanya tidak
pernah membuka ``.env``. ``bootstrap-roles`` menulis kedua file. ``serve``
hanya membaca ``.env``. Lihat :mod:`payment_guard.config`.

``verify`` tidak menyentuh database, jaringan, maupun chain, dan tidak punya
jalur untuk membaca bahan kunci rahasia (:mod:`payment_guard.verify_cli`).

``approvals`` berjalan sebagai role approver dan hanya di terminal interaktif
(Security v1.1 E3, ST-23). ``execute`` dan ``recover`` berjalan sebagai role
executor dengan kunci wallet sendiri, dan tidak pernah membaca kunci penanda
tangan Guard. Perintah runtime lainnya (``watch``, ``reconcile``) menyusul di
milestone berikutnya.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from payment_guard import __version__
from payment_guard.bootstrap import read_admin_url, run_bootstrap_roles
from payment_guard.config import (
    MIGRATOR_ENV_FILENAME,
    RUNTIME_ENV_FILENAME,
    GuardConfigError,
    require_migrator_url,
)
from payment_guard.db import MigrationError
from payment_guard.keygen import (
    PUBLIC_KEYS_RELATIVE_PATH,
    SIGNING_KEY_RELATIVE_PATH,
    KeygenError,
    run_keygen,
)
from payment_guard.migrate import migrations_dir, run_migrate, run_status
from payment_guard.verify_cli import describe, verify_envelope_file


def _repo_root() -> Path:
    """Akar repo, yaitu direktori yang memuat ``migrations/``."""
    return Path(__file__).resolve().parents[2]


def _cmd_migrate(args: argparse.Namespace) -> int:
    applied = run_migrate(require_migrator_url(args.migrator_env_file), args.migrations_dir)
    if not applied:
        print("Tidak ada migrasi baru. Skema sudah mutakhir.")
    else:
        for file in applied:
            print(f"diterapkan  {file.label}  ({file.checksum[:12]}...)")
        print(f"{len(applied)} migrasi diterapkan.")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    report = run_status(require_migrator_url(args.migrator_env_file), args.migrations_dir)
    if not report.table_exists:
        print("schema_migrations belum ada: belum pernah ada migrasi yang dijalankan.")
    for row in report.applied:
        print(f"terapan   {row.version:04d}_{row.name}.sql  {row.applied_at:%Y-%m-%d %H:%M:%S%z}")
    for file in report.pending:
        print(f"tertunda  {file.label}")
    print(f"\n{len(report.applied)} terapan, {len(report.pending)} tertunda.")
    return 0


def _cmd_bootstrap_roles(args: argparse.Namespace) -> int:
    admin_url = read_admin_url(allow_prompt=not args.no_prompt)
    result = run_bootstrap_roles(
        admin_url,
        args.env_file,
        args.migrator_env_file,
        allow_remote_admin=args.allow_remote_admin,
    )
    # admin_url tidak pernah dicetak, dan tidak disimpan di mana pun.
    del admin_url
    for line in result.summary_lines():
        print(line)
    print("\nPassword role tidak ditampilkan dengan sengaja. Lanjutkan dengan `guard db migrate`.")
    return 0


def _cmd_keygen(args: argparse.Namespace) -> int:
    result = run_keygen(args.signing_key_file, args.public_keys_file)
    for line in result.summary_lines():
        print(line)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    # Diimpor di dalam handler, bukan di atas: modul ini menarik FastMCP dan
    # seluruh jalur keputusan, dan `guard verify` tidak boleh ikut memuatnya.
    # Verify dirancang bekerja tanpa database dan tanpa jaringan; impor di
    # tingkat modul akan menghapus sifat itu tanpa mengubah satu baris pun
    # di verify.
    from payment_guard.mcp_server import serve

    return serve(args.env_file, repo_root=_repo_root())


def _cmd_approvals(args: argparse.Namespace) -> int:
    # Lazy seperti `serve`, dan karena alasan yang sama: modul ini menarik
    # psycopg, jalur keputusan, dan bahan tanda tangan. `guard verify` harus
    # tetap jalan tanpa database dan tanpa jaringan.
    from payment_guard.approvals_cli import (
        ApprovalError,
        approve,
        list_pending,
        reject,
    )
    from payment_guard.config import load_approvals_config

    try:
        config = load_approvals_config(args.env_file, repo_root=_repo_root())
    except GuardConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        if args.command == "list":
            return list_pending(config)
        action = approve if args.command == "approve" else reject
        action(config, args.decision_id)
    except ApprovalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_execute(args: argparse.Namespace) -> int:
    # Lazy seperti `serve` dan `approvals`, dan karena alasan tambahan: modul
    # ini menarik web3 dan eth-account, yang mahal diimpor. `guard verify`
    # tidak boleh ikut memuat tumpukan chain hanya untuk memeriksa satu berkas.
    from payment_guard.chain import ChainError
    from payment_guard.config import load_executor_config
    from payment_guard.executor import ExecutorError, run_execute

    try:
        config = load_executor_config(args.env_file, repo_root=_repo_root())
    except GuardConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        return run_execute(config, args.decision_id)
    except (ChainError, ExecutorError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_recover(args: argparse.Namespace) -> int:
    from payment_guard.chain import ChainError
    from payment_guard.config import load_executor_config
    from payment_guard.executor import ExecutorError, run_recover

    try:
        config = load_executor_config(args.env_file, repo_root=_repo_root())
    except GuardConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        return run_recover(config)
    except (ChainError, ExecutorError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_watch(args: argparse.Namespace) -> int:
    # Lazy seperti `execute`: modul ini menarik web3, yang mahal diimpor.
    from payment_guard.chain import ChainError
    from payment_guard.config import load_watcher_config
    from payment_guard.watcher import WatcherError, run_watch

    try:
        config = load_watcher_config(args.env_file)
    except GuardConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        return run_watch(
            config,
            loop=args.loop,
            cycles=args.cycles,
            from_block=args.from_block,
            out=sys.stdout,
        )
    except KeyboardInterrupt:
        # `guard watch --loop` dihentikan operator dengan Ctrl-C, dan itu jalur
        # normal, bukan kegagalan. Koneksi dan lock sesinya sudah dilepas oleh
        # `finally` di run_watch.
        print("dihentikan", file=sys.stderr)
        return 0
    except (ChainError, WatcherError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_reconcile(args: argparse.Namespace) -> int:
    # Tanpa impor chain sama sekali: Reconciler membandingkan tabel dengan
    # tabel dan tidak pernah bertanya ke RPC.
    from payment_guard.config import load_reconciler_config
    from payment_guard.reconciler import ReconcilerError, run_reconcile, run_report

    try:
        config = load_reconciler_config(args.env_file)
    except GuardConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        if args.report:
            # Jalur baca saja: menghitung ulang dari tabel, tanpa menulis satu
            # baris pun ke `reconciliations` maupun `audit_log`.
            return run_report(config, out=sys.stdout)
        return run_reconcile(config, out=sys.stdout)
    except ReconcilerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_wallet(args: argparse.Namespace) -> int:
    """Cetak alamat wallet Executor. Satu baris, tanpa menyentuh apa pun.

    Tidak ada koneksi database, tidak ada RPC, tidak ada transaksi yang
    dibangun maupun disiarkan. Ia membaca satu berkas dan menurunkan satu
    alamat dari kuncinya.

    Alasan perintah ini ada: sebelum M6.3.1, checklist operator menyuruh
    menjalankan ``guard execute`` untuk **membaca** alamat wallet dari laporan
    startup-nya. ``guard execute`` menyiarkan transaksi bila seluruh ceknya
    lolos, dan ia tidak punya mode baca-saja. Memakai perintah yang
    membelanjakan uang untuk membaca satu alamat adalah kekeliruan yang hanya
    perlu terjadi sekali.
    """
    # Lazy: modul chain menarik web3 dan eth-account, yang mahal diimpor.
    # Keduanya tidak membuka koneksi apa pun saat diimpor maupun saat
    # menurunkan alamat dari kunci.
    from payment_guard.chain import ChainError, load_wallet
    from payment_guard.config import load_wallet_key_path

    try:
        path = load_wallet_key_path(args.env_file, repo_root=_repo_root())
    except GuardConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        wallet = load_wallet(path)
    except ChainError as exc:
        # Pesan `ChainError` dari `load_wallet` menyebut path dan alasannya,
        # dan tidak pernah bahan kuncinya; lihat docstring-nya.
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Satu baris, dan hanya alamatnya. Bentuknya sama dengan baris yang
    # dicetak laporan startup Executor, supaya keduanya bisa dibandingkan
    # tanpa menafsirkan apa pun.
    print(f"wallet {wallet.address}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    outcome = verify_envelope_file(args.envelope, args.pubkey, args.mandate)
    print(outcome.render())
    if outcome.valid and outcome.payload is not None:
        for line in describe(outcome.payload):
            print(line)
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="guard",
        description="Payment Guard — testnet-only reference implementation.",
    )
    parser.add_argument("--version", action="version", version=f"payment-guard {__version__}")
    sub = parser.add_subparsers(dest="group", required=True)

    db = sub.add_parser("db", help="migrasi dan role database")
    db_sub = db.add_subparsers(dest="command", required=True)

    migrate_parser = db_sub.add_parser("migrate", help="terapkan migrasi yang belum terapan")
    migrate_parser.set_defaults(handler=_cmd_migrate)

    status_parser = db_sub.add_parser("status", help="laporkan keadaan migrasi")
    status_parser.set_defaults(handler=_cmd_status)

    for item in (migrate_parser, status_parser):
        item.add_argument(
            "--migrations-dir",
            type=Path,
            default=None,
            help="direktori file migrasi (default: migrations/ di akar repo)",
        )

    bootstrap_parser = db_sub.add_parser(
        "bootstrap-roles",
        help="buat role database (admin, sekali). URL admin dari env sementara atau prompt.",
    )
    bootstrap_parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=(
            f"file URL runtime, lima proses (default: {RUNTIME_ENV_FILENAME} di akar repo). "
            "Tidak memuat URL migrator."
        ),
    )
    bootstrap_parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="jangan meminta URL admin lewat prompt; wajib lewat environment variable",
    )
    bootstrap_parser.add_argument(
        "--allow-remote-admin",
        action="store_true",
        help=(
            "izinkan URL admin yang hostnya bukan loopback. Secara bawaan ditolak: "
            "bootstrap membuat role dan mengubah hak tingkat schema."
        ),
    )
    bootstrap_parser.set_defaults(handler=_cmd_bootstrap_roles)

    # URL migrator hidup di filenya sendiri, terpisah dari URL runtime.
    for item in (migrate_parser, status_parser, bootstrap_parser):
        item.add_argument(
            "--migrator-env-file",
            type=Path,
            default=None,
            help=(f"file berisi MIGRATOR_DB_URL (default: {MIGRATOR_ENV_FILENAME} di akar repo)"),
        )

    # --- keygen: menulis bahan kunci, jadi bukan bagian dari `db` ---
    keygen_parser = sub.add_parser(
        "keygen",
        help="buat pasangan kunci Ed25519 (sekali). Tidak pernah menimpa yang sudah ada.",
    )
    keygen_parser.add_argument(
        "--signing-key-file",
        type=Path,
        default=None,
        help=(
            f"tujuan bahan kunci rahasia (default: {SIGNING_KEY_RELATIVE_PATH} "
            "di akar repo). Di-gitignore; jangan di-commit."
        ),
    )
    keygen_parser.add_argument(
        "--public-keys-file",
        type=Path,
        default=None,
        help=(
            f"registri kunci publik (default: {PUBLIC_KEYS_RELATIVE_PATH} di akar "
            "repo). Boleh di-commit."
        ),
    )
    keygen_parser.set_defaults(handler=_cmd_keygen)

    # --- serve: server MCP stdio, satu proses satu agent ---
    serve_parser = sub.add_parser(
        "serve",
        help="jalankan Guard sebagai server MCP stdio (dua tool, transport stdio)",
    )
    serve_parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=(
            f"file konfigurasi runtime Guard (default: {RUNTIME_ENV_FILENAME} di akar "
            "repo). Tidak pernah membaca .env.migrator."
        ),
    )
    serve_parser.set_defaults(handler=_cmd_serve)

    # --- approvals: hanya operator, hanya interaktif (ST-23) ---
    approvals_parser = sub.add_parser(
        "approvals",
        help="daftar, setujui, atau tolak keputusan yang menunggu manusia",
    )
    approvals_parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=(
            f"file konfigurasi runtime (default: {RUNTIME_ENV_FILENAME} di akar repo). "
            "Memakai APPROVER_DB_URL, bukan URL Guard."
        ),
    )
    approvals_sub = approvals_parser.add_subparsers(dest="command", required=True)
    approvals_sub.add_parser("list", help="tampilkan keputusan yang menunggu persetujuan")
    for name, bantuan in (
        ("approve", "setujui satu keputusan; batas dicek ulang dengan data terbaru"),
        ("reject", "tolak satu keputusan; memicu cooldown untuk penerima itu"),
    ):
        item = approvals_sub.add_parser(name, help=bantuan)
        item.add_argument("decision_id", help="UUID dari jawaban check_payment")
    approvals_parser.set_defaults(handler=_cmd_approvals)

    # --- execute dan recover: Executor, role executor, kunci wallet sendiri ---
    #
    # Tidak ada flag konfirmasi dan tidak ada `--yes`: yang menyetujui
    # pembayaran adalah operator di `guard approvals approve`, dan keputusan itu
    # sudah bertanda tangan. Di sini tidak ada lagi yang boleh dipilih —
    # `execute` hanya memproses keputusan `approved` yang belum punya
    # execution, dan menolak apa pun selain itu.
    execute_parser = sub.add_parser(
        "execute",
        help="eksekusi satu keputusan approved ke chain (write-ahead lalu broadcast)",
    )
    execute_parser.add_argument(
        "decision_id", help="UUID keputusan approved yang belum punya execution"
    )
    execute_parser.set_defaults(handler=_cmd_execute)

    recover_parser = sub.add_parser(
        "recover",
        help="selesaikan execution yang tertinggal (signed atau broadcast). Idempoten.",
    )
    recover_parser.set_defaults(handler=_cmd_recover)

    for item in (execute_parser, recover_parser):
        item.add_argument(
            "--env-file",
            type=Path,
            default=None,
            help=(
                f"file konfigurasi runtime (default: {RUNTIME_ENV_FILENAME} di akar repo). "
                "Memakai EXECUTOR_DB_URL, EXECUTOR_KEY_PATH, dan RPC_URL."
            ),
        )

    # --- watch dan reconcile: keduanya read-only terhadap chain (M6) ---
    #
    # `watch` membaca log dan menulis `settlements`; ia tidak pernah
    # menandatangani, tidak pernah menyiarkan, dan tidak punya hak baca ke
    # `decisions` maupun `executions`. `reconcile` tidak menyentuh chain sama
    # sekali: ia membandingkan tabel dengan tabel.
    watch_parser = sub.add_parser(
        "watch",
        help="pindai log Transfer USDC dari wallet Executor menjadi settlements",
    )
    watch_parser.add_argument(
        "--loop",
        action="store_true",
        help=(
            "jalankan terus dengan jeda WATCHER_INTERVAL_SECONDS. "
            "Tanpa ini, satu siklus lalu keluar."
        ),
    )
    watch_parser.add_argument(
        "--cycles",
        type=int,
        default=None,
        metavar="N",
        help="batasi jumlah siklus (dipakai operator untuk mencoba, dan oleh tes)",
    )
    watch_parser.add_argument(
        "--from-block",
        type=int,
        default=None,
        metavar="BLOK",
        help=(
            "blok awal, HANYA bila belum ada kursor di watcher_state. Kursor yang "
            "sudah ada tidak pernah dipindahkan ke depan oleh flag ini: itu akan "
            "melewatkan blok dalam diam. Lihat docs/runbook.md bagian 8."
        ),
    )
    watch_parser.set_defaults(handler=_cmd_watch)

    reconcile_parser = sub.add_parser(
        "reconcile",
        help="cocokkan settlements dengan executions dan decisions; satu siklus",
    )
    reconcile_parser.add_argument(
        "--report",
        action="store_true",
        help=(
            "hanya tampilkan laporan, jangan tulis apa pun. Menghitung ulang dari "
            "settlements, executions, dan decisions; tidak menulis ke "
            "reconciliations maupun audit_log."
        ),
    )
    reconcile_parser.set_defaults(handler=_cmd_reconcile)

    watch_parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=(
            f"file konfigurasi runtime (default: {RUNTIME_ENV_FILENAME} di akar repo). "
            "Memakai WATCHER_DB_URL, RPC_URL, dan EXECUTOR_WALLET_ADDRESS."
        ),
    )
    reconcile_parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=(
            f"file konfigurasi runtime (default: {RUNTIME_ENV_FILENAME} di akar repo). "
            "Memakai RECONCILER_DB_URL saja; tidak ada RPC_URL."
        ),
    )

    # --- wallet: baca satu berkas, cetak satu alamat. Tidak lebih. ---
    #
    # SENGAJA tanpa argumen selain `--env-file`. Tidak ada `--yes`, tidak ada
    # `--broadcast`, tidak ada mode lain: perintah yang hanya bisa melakukan
    # satu hal tidak bisa disuruh melakukan hal lain, dan itulah seluruh
    # gunanya dibanding membaca alamat dari laporan startup `guard execute`.
    wallet_parser = sub.add_parser(
        "wallet",
        help=(
            "cetak alamat wallet Executor dari berkas kuncinya; "
            "tanpa database, tanpa jaringan, tanpa transaksi"
        ),
    )
    wallet_parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=(
            f"file konfigurasi runtime (default: {RUNTIME_ENV_FILENAME} di akar repo). "
            "Hanya EXECUTOR_KEY_PATH yang dibaca."
        ),
    )
    wallet_parser.set_defaults(handler=_cmd_wallet)

    # --- verify: hanya kunci publik, tanpa database dan tanpa jaringan ---
    verify_parser = sub.add_parser(
        "verify",
        help="periksa envelope dengan kunci publik saja; tanpa database atau jaringan",
    )
    verify_parser.add_argument("envelope", type=Path, help="berkas envelope JSON")
    verify_parser.add_argument(
        "--pubkey",
        type=Path,
        default=None,
        help=f"registri kunci publik (default: {PUBLIC_KEYS_RELATIVE_PATH} di akar repo)",
    )
    verify_parser.add_argument(
        "--mandate",
        type=Path,
        default=None,
        help=(
            "opsional: cocokkan mandate_hash di payload dengan hash berkas mandate ini, "
            "supaya envelope yang sah atas aturan yang salah tidak lolos"
        ),
    )
    verify_parser.set_defaults(handler=_cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    root = _repo_root()
    # Setiap default hanya diisi untuk perintah yang memang mendeklarasikan
    # argumennya. Perintah yang tidak mendeklarasikannya tidak punya
    # atributnya sama sekali, jadi tidak ada path yang bisa dibacanya tanpa
    # sengaja: `verify` tidak punya path `.env`, dan `db` tidak punya path
    # bahan kunci.
    for name, default in (
        ("migrations_dir", migrations_dir(root)),
        ("env_file", root / RUNTIME_ENV_FILENAME),
        ("migrator_env_file", root / MIGRATOR_ENV_FILENAME),
        ("signing_key_file", root / SIGNING_KEY_RELATIVE_PATH),
        ("public_keys_file", root / PUBLIC_KEYS_RELATIVE_PATH),
        ("pubkey", root / PUBLIC_KEYS_RELATIVE_PATH),
    ):
        if hasattr(args, name) and getattr(args, name) is None:
            setattr(args, name, default)

    try:
        return int(args.handler(args))
    except (MigrationError, KeygenError) as exc:
        # Pesan keduanya sudah bebas rahasia; dicetak apa adanya.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ndibatalkan.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
