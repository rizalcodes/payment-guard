"""Entry point ``guard``.

Perintah yang tersedia sampai M2:

    guard db migrate          terapkan migrasi yang belum terapan (role migrator)
    guard db status           laporkan migrasi terapan dan tertunda
    guard db bootstrap-roles  buat role database (admin, sekali)
    guard keygen              buat pasangan kunci Ed25519 (sekali)
    guard verify              periksa envelope dengan kunci publik saja

``migrate`` dan ``status`` hanya membaca ``.env.migrator``; keduanya tidak
pernah membuka ``.env``. ``bootstrap-roles`` menulis kedua file. Lihat
:mod:`payment_guard.config`.

``verify`` tidak menyentuh database, jaringan, maupun chain, dan tidak punya
jalur untuk membaca bahan kunci rahasia (:mod:`payment_guard.verify_cli`).

Perintah runtime (``serve``, ``execute``, ``approvals``, ``watch``,
``reconcile``) menyusul di milestone berikutnya.
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
