"""Entry point ``guard``.

Hanya sub-perintah ``db`` yang ada di M0:

    guard db migrate          terapkan migrasi yang belum terapan (role migrator)
    guard db status           laporkan migrasi terapan dan tertunda
    guard db bootstrap-roles  buat role database (admin, sekali)

``migrate`` dan ``status`` hanya membaca ``.env.migrator``; keduanya tidak
pernah membuka ``.env``. ``bootstrap-roles`` menulis kedua file. Lihat
:mod:`payment_guard.config`.

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
from payment_guard.migrate import migrations_dir, run_migrate, run_status


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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    root = _repo_root()
    if getattr(args, "migrations_dir", None) is None:
        args.migrations_dir = migrations_dir(root)
    # `env_file` hanya diisi untuk perintah yang memang mendeklarasikannya.
    # migrate dan status tidak punya atribut itu sama sekali, jadi tidak ada
    # path `.env` yang bisa mereka baca walau tidak sengaja.
    if hasattr(args, "env_file") and args.env_file is None:
        args.env_file = root / RUNTIME_ENV_FILENAME
    if getattr(args, "migrator_env_file", None) is None:
        args.migrator_env_file = root / MIGRATOR_ENV_FILENAME

    try:
        return int(args.handler(args))
    except MigrationError as exc:
        # Pesan MigrationError sudah bebas rahasia; dicetak apa adanya.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ndibatalkan.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
