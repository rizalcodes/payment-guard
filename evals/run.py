"""CLI eval: ``python -m evals.run``.

Setiap **run** membangun lingkungan sekali pakai di cluster **tes**, menjalankan
satu skenario, lalu menghapus databasenya. Satu lingkungan per run, bukan per
sesi: lihat komentar di :func:`_run_session` untuk kenapa berbagi lingkungan
merusak apa yang diukur. Hasilnya ditulis ke ``evals/results/<stempel-UTC>/``.

Tiga penjaga, dan semuanya menolak jalan alih-alih memperingatkan:

1. **Anggaran** diperiksa sebelum tiap run, bukan sesudah. Run yang
   diperkirakan melewati batas tidak pernah dikirim.
2. **Arm API menunggu izin eksplisit.** ``--model haiku`` saja tidak cukup; ia
   butuh ``--live-api`` juga. Dua bendera untuk membelanjakan uang sungguhan
   adalah harga yang murah.
3. **Cluster operator ditolak**, oleh :func:`evals.environment.require_test_cluster`.

``--dry-run`` mencetak rencananya dan tidak memanggil model, Guard, maupun
database.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from evals.adapters import (
    EVAL_API_KEY_VAR,
    HAIKU_MODEL_ID,
    OLLAMA_MODEL_ID,
    HaikuAdapter,
    ModelAdapter,
    OllamaAdapter,
)
from evals.budget import BudgetExceeded, BudgetGuard, format_micro_usd, usd_to_micro
from evals.environment import TEST_ADMIN_URL_ENV, EnvironmentError_, eval_environment
from evals.harness import BroadcastAttempted, run_once
from evals.results import RESULTS_ROOT, SessionWriter
from evals.scenarios import AGENT_ID, EVAL_MANDATE, SCENARIOS, scenario_by_code

#: Pilihan ``--model``.
MODEL_OLLAMA = "ollama"
MODEL_HAIKU = "haiku"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run",
        description="Jalankan eval E1-E4 Payment Guard terhadap Guard sungguhan.",
    )
    parser.add_argument(
        "--model",
        choices=(MODEL_OLLAMA, MODEL_HAIKU),
        default=MODEL_OLLAMA,
        help=f"model yang diuji (bawaan: {MODEL_OLLAMA})",
    )
    parser.add_argument(
        "--live-api",
        action="store_true",
        help="izin eksplisit memanggil API berbayar. Wajib untuk --model haiku.",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        metavar="KODE",
        help="skenario yang dijalankan (E1..E4). Bisa diulang. Bawaan: semuanya.",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="berapa kali tiap skenario dijalankan (bawaan: 1)",
    )
    parser.add_argument(
        "--max-runs",
        type=int,
        default=None,
        help="batas keras jumlah run dalam sesi ini",
    )
    parser.add_argument(
        "--budget-usd",
        type=int,
        default=None,
        help="anggaran keras dalam USD bulat (bawaan: 5)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="cetak rencana lalu berhenti. Tanpa model, tanpa Guard, tanpa database.",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=None,
        help=f"akar direktori hasil (bawaan: {RESULTS_ROOT})",
    )
    return parser


def _planned_runs(codes: list[str], repeats: int, max_runs: int | None) -> list[str]:
    """Daftar kode skenario yang akan dijalankan, dalam urutannya.

    Urutannya skenario-dulu lalu ulangan (E1 E2 E3 E4 E1 E2 ...), bukan
    ulangan-dulu. Dengan ``--max-runs`` kecil, urutan ini memberi satu run
    untuk setiap skenario sebelum ada skenario yang diulang; urutan sebaliknya
    akan menghabiskan batas pada E1 saja.
    """
    rencana: list[str] = []
    for _ in range(repeats):
        rencana.extend(codes)
    if max_runs is not None:
        rencana = rencana[:max_runs]
    return rencana


def _build_adapter(pilihan: str, *, live_api: bool) -> ModelAdapter:
    """Bangun adapter, atau tolak bila izin API belum diberikan.

    Raises:
        SystemExit: bila ``haiku`` dipilih tanpa ``--live-api``, atau bila
            kuncinya tidak ada.
    """
    if pilihan == MODEL_OLLAMA:
        return OllamaAdapter()
    if not live_api:
        raise SystemExit(
            f"--model {MODEL_HAIKU} memanggil API berbayar. Tambahkan --live-api "
            "bila itu memang yang diminta pemilik."
        )
    if not (os.environ.get(EVAL_API_KEY_VAR) or "").strip():
        raise SystemExit(f"{EVAL_API_KEY_VAR} belum disetel di lingkungan runner eval.")
    return HaikuAdapter.from_env()


def _model_metadata(adapter: ModelAdapter) -> list[dict[str, str]]:
    return [
        {
            "nama": adapter.model_name,
            "versi": adapter.model_version,
            "suhu": adapter.temperature,
        }
    ]


def _print_plan(
    *,
    out: TextIO,
    pilihan: str,
    rencana: list[str],
    budget: BudgetGuard,
    dry_run: bool,
) -> None:
    label = {MODEL_OLLAMA: OLLAMA_MODEL_ID, MODEL_HAIKU: HAIKU_MODEL_ID}[pilihan]
    print(f"Model     : {label}", file=out)
    print(f"Run       : {len(rencana)} ({' '.join(rencana) or 'tidak ada'})", file=out)
    print(f"Anggaran  : {format_micro_usd(budget.limit_micro_usd)}", file=out)
    print(
        f"Efektif   : {format_micro_usd(budget.usable_micro_usd)} "
        f"(cadangan {budget.reserve_percent}%)",
        file=out,
    )
    print(f"Mode      : {'dry-run, tidak ada panggilan' if dry_run else 'jalan'}", file=out)


async def _run_session(
    *,
    adapter: ModelAdapter,
    codes: list[str],
    budget: BudgetGuard,
    writer: SessionWriter,
    admin_url: str,
    out: TextIO,
) -> tuple[int, list[str]]:
    """Jalankan rencana. Mengembalikan ``(jumlah_run, catatan)``."""
    catatan: list[str] = []
    jalan = 0

    with tempfile.TemporaryDirectory(prefix="payment-guard-eval-") as tmp:
        for nomor, kode in enumerate(codes, start=1):
            try:
                budget.check_before_run()
            except BudgetExceeded as exc:
                catatan.append(f"berhenti karena anggaran: {exc}")
                print(f"BERHENTI: {exc}", file=out)
                break

            skenario = scenario_by_code(kode)

            # Satu lingkungan per **run**, bukan per sesi. Lingkungan yang
            # dipakai bersama membawa keadaan dari run sebelumnya, dan keadaan
            # yang paling merusak adalah cooldown: run pertama E3 menghasilkan
            # ``denied (recipient_not_allowed)``, yang memicu cooldown untuk
            # penerima itu, sehingga sembilan run sesudahnya menerima
            # ``denied (retry_blocked)`` lebih dulu dan tidak lagi menguji
            # aturan yang dimaksud. Diukur: 9 dari 10 run E3 di sesi
            # 20261111T142233Z.
            #
            # ``eval_environment`` sudah membuat database sendiri per panggilan
            # (``evalrun_<hex>_test``) dan menghapusnya di ``finally``, jadi
            # yang dibutuhkan hanya memindahkan pemanggilannya ke dalam loop.
            # Direktori kerjanya dipisah per run supaya kunci, mandate, dan
            # berkas env satu run tidak menimpa run lain.
            with eval_environment(admin_url, Path(tmp) / f"run-{nomor:03d}", EVAL_MANDATE) as env:
                hasil = await run_once(
                    adapter=adapter,
                    scenario=skenario,
                    env_file=env.env_file,
                    public_keys_path=env.public_keys_path,
                    admin_url=env.admin_url,
                    agent_id=AGENT_ID,
                    expected_mandate_hash=env.mandate_hash,
                )

            budget.record(hasil.cost_micro_usd)
            writer.add(hasil)
            jalan += 1
            print(
                f"{kode} {hasil.verdict}: {hasil.reason} "
                f"[{format_micro_usd(hasil.cost_micro_usd)}]",
                file=out,
            )
    return jalan, catatan


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None) -> int:
    """Titik masuk ``python -m evals.run``."""
    aliran = out or sys.stdout
    args = build_parser().parse_args(argv)

    kode_tersedia = [s.code for s in SCENARIOS]
    codes = list(args.scenario) if args.scenario else list(kode_tersedia)
    for kode in codes:
        if kode not in kode_tersedia:
            print(f"skenario {kode!r} tidak ada; yang ada: {kode_tersedia}", file=aliran)
            return 2
    if args.repeats < 1:
        print("--repeats harus >= 1", file=aliran)
        return 2
    if args.max_runs is not None and args.max_runs < 1:
        print("--max-runs harus >= 1", file=aliran)
        return 2

    try:
        batas = usd_to_micro(args.budget_usd) if args.budget_usd is not None else None
    except ValueError as exc:
        print(f"--budget-usd: {exc}", file=aliran)
        return 2
    budget = BudgetGuard() if batas is None else BudgetGuard(limit_micro_usd=batas)

    rencana = _planned_runs(codes, args.repeats, args.max_runs)
    _print_plan(
        out=aliran, pilihan=args.model, rencana=rencana, budget=budget, dry_run=args.dry_run
    )
    if args.dry_run:
        print("dry-run: tidak ada yang dijalankan.", file=aliran)
        return 0

    try:
        adapter = _build_adapter(args.model, live_api=args.live_api)
    except SystemExit as exc:
        print(str(exc), file=aliran)
        return 2

    admin_url = (os.environ.get(TEST_ADMIN_URL_ENV) or "").strip()
    if not admin_url:
        print(
            f"{TEST_ADMIN_URL_ENV} belum disetel. Eval hanya berjalan di cluster tes.",
            file=aliran,
        )
        return 2

    base = args.results_dir or RESULTS_ROOT
    writer = SessionWriter.create(base)
    mulai = datetime.now(UTC)

    try:
        jalan, catatan = asyncio.run(
            _run_session(
                adapter=adapter,
                codes=rencana,
                budget=budget,
                writer=writer,
                admin_url=admin_url,
                out=aliran,
            )
        )
    except BroadcastAttempted as exc:
        # Tidak ditangani sebagai kegagalan satu run: asumsi dasar eval sudah
        # tidak berlaku, dan run berikutnya akan mengukur sesuatu yang tidak
        # dipahami. Sesi berhenti, hasil yang sudah ada tetap ditulis.
        print(f"EVAL BERHENTI TOTAL: {exc}", file=aliran)
        writer.write_summary(budget=budget, notes=[f"berhenti total: {exc}"])
        return 3
    except EnvironmentError_ as exc:
        print(f"lingkungan eval ditolak: {exc}", file=aliran)
        return 2

    writer.write_metadata(
        models=_model_metadata(adapter),
        budget=budget,
        repo_root=Path.cwd(),
        dry_run=False,
        api_key_available=bool((os.environ.get(EVAL_API_KEY_VAR) or "").strip()),
        started=mulai,
    )
    ringkasan = writer.write_summary(budget=budget, notes=catatan)
    print("", file=aliran)
    print(ringkasan, file=aliran)
    print(f"Hasil: {writer.root}", file=aliran)
    return 0 if jalan else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
