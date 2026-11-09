"""Menulis hasil eval ke disk, dalam bentuk yang bisa dibaca ulang.

Struktur satu sesi, di bawah ``evals/results/<stempel-UTC>/``::

    metadata.json      model, versi, suhu, waktu, commit, biaya
    summary.md         angka mentah per skenario per model
    runs.jsonl         satu baris JSON per run
    transcripts/       satu berkas per run, percakapan lengkap

Direktori ``evals/results/`` ada di ``.gitignore``. Itu disengaja: hasil run
adalah data, bukan kode, dan transkrip bisa memuat teks model yang panjang.

**Tidak ada kunci API yang ditulis ke mana pun di sini.** Yang dicatat tentang
kredensial hanyalah apakah sebuah kunci tersedia — ``true`` atau ``false``.
Ada tes yang mencari nilai kunci tiruan di seluruh berkas keluaran.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from evals.adapters import redact
from evals.budget import BudgetGuard, format_micro_usd
from evals.harness import RunResult
from evals.scoring import Tally, Verdict

#: Nama berkas, satu tempat supaya tes dan penulis tidak bisa berbeda.
METADATA_FILENAME: Final = "metadata.json"
SUMMARY_FILENAME: Final = "summary.md"
RUNS_FILENAME: Final = "runs.jsonl"
TRANSCRIPTS_DIRNAME: Final = "transcripts"

#: Akar hasil, relatif terhadap akar repo.
RESULTS_ROOT: Final = Path("evals") / "results"

#: Bentuk stempel waktu direktori. Aman untuk nama berkas di Windows:
#: titik dua tidak dipakai.
TIMESTAMP_FORMAT: Final = "%Y%m%dT%H%M%SZ"


def utc_stamp(now: datetime | None = None) -> str:
    """Stempel UTC untuk nama direktori sesi."""
    saat = now or datetime.now(UTC)
    return saat.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def git_commit(repo_root: Path) -> str:
    """HEAD pendek, atau ``"unknown"`` bila git tidak bisa dipanggil.

    Dicatat supaya satu angka hasil bisa dikaitkan ke kode yang
    menghasilkannya. Kegagalan di sini tidak boleh membatalkan sesi eval yang
    sudah membelanjakan uang, jadi ia mengembalikan ``"unknown"`` alih-alih
    melempar.
    """
    try:
        hasil = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return hasil.stdout.strip() or "unknown"


@dataclass
class SessionWriter:
    """Satu direktori sesi, dibuka sekali dan ditulis bertahap.

    Ditulis bertahap dengan sengaja: ``runs.jsonl`` ditambah tiap run selesai,
    jadi sesi yang terhenti di tengah — anggaran habis, Ctrl-C, Ollama mati —
    tetap meninggalkan hasil run yang sudah dibayar.
    """

    root: Path
    tallies: dict[tuple[str, str], Tally] = field(default_factory=dict)
    run_count: int = 0

    @classmethod
    def create(cls, base: Path, *, now: datetime | None = None) -> SessionWriter:
        """Buat direktori sesi baru di bawah ``base``."""
        root = base / utc_stamp(now)
        (root / TRANSCRIPTS_DIRNAME).mkdir(parents=True, exist_ok=True)
        return cls(root=root)

    @property
    def runs_path(self) -> Path:
        return self.root / RUNS_FILENAME

    @property
    def summary_path(self) -> Path:
        return self.root / SUMMARY_FILENAME

    @property
    def metadata_path(self) -> Path:
        return self.root / METADATA_FILENAME

    def add(self, result: RunResult) -> None:
        """Catat satu run: baris JSONL, transkrip, dan hitungannya."""
        self.run_count += 1
        kunci = (result.scenario_code, result.model_name)
        tally = self.tallies.setdefault(
            kunci, Tally(scenario_code=result.scenario_code, model_name=result.model_name)
        )
        tally.add(result.verdict, attempted=result.attempted, bypass=result.bypasses)

        baris = {
            "nomor": self.run_count,
            "skenario": result.scenario_code,
            "model": result.model_name,
            "verdict": str(result.verdict),
            "alasan": result.reason,
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "biaya_micro_usd": result.cost_micro_usd,
            "biaya": format_micro_usd(result.cost_micro_usd),
            "attempted_variation": result.attempted,
            "bypasses": result.bypasses,
            "tool_calls": [c.name for c in result.observation.tool_calls],
            "eksekusi": [
                {
                    "decision_id": e.decision_id,
                    "verified": e.verified,
                    "broadcast": e.broadcast,
                    "detail": e.detail,
                }
                for e in result.observation.executions
            ],
            "mandate_hash_berubah": (
                result.observation.mandate_hash_before != result.observation.mandate_hash_after
            ),
            "refused": result.observation.refused,
        }
        # Disunting saat tulis, bukan diandalkan tidak ada. Kunci tidak pernah
        # masuk prompt, jadi model tidak punya cara mengucapkannya - tapi
        # "tidak punya cara" adalah kesimpulan dari membaca kode hari ini,
        # dan berkas hasil hidup lebih lama daripada kesimpulan itu.
        with self.runs_path.open("a", encoding="utf-8") as berkas:
            berkas.write(redact(json.dumps(baris, ensure_ascii=False, default=str)) + "\n")

        nama = f"{self.run_count:03d}-{result.scenario_code}-{_slug(result.model_name)}.json"
        transkrip = {
            "skenario": result.scenario_code,
            "model": result.model_name,
            "verdict": str(result.verdict),
            "alasan": result.reason,
            "percakapan": result.transcript,
            "teks_akhir": result.observation.final_text,
        }
        (self.root / TRANSCRIPTS_DIRNAME / nama).write_text(
            redact(json.dumps(transkrip, ensure_ascii=False, indent=2, default=str)),
            encoding="utf-8",
        )

    def write_metadata(
        self,
        *,
        models: list[dict[str, str]],
        budget: BudgetGuard,
        repo_root: Path,
        dry_run: bool,
        api_key_available: bool,
        started: datetime,
        finished: datetime | None = None,
    ) -> None:
        """Tulis ``metadata.json``. Tanpa kunci, tanpa URL database."""
        isi = {
            "mulai": started.astimezone(UTC).isoformat(),
            "selesai": (finished or datetime.now(UTC)).astimezone(UTC).isoformat(),
            "commit": git_commit(repo_root),
            "dry_run": dry_run,
            "model": models,
            "anggaran": {
                "batas_micro_usd": budget.limit_micro_usd,
                "batas": format_micro_usd(budget.limit_micro_usd),
                "cadangan_persen": budget.reserve_percent,
                "batas_efektif": format_micro_usd(budget.usable_micro_usd),
                "terpakai_micro_usd": budget.spent_micro_usd,
                "terpakai": format_micro_usd(budget.spent_micro_usd),
                "run": budget.runs,
            },
            # Hanya ketersediaannya, bukan nilainya. Tidak pernah nilainya.
            "api_key_tersedia": api_key_available,
            "jumlah_run": self.run_count,
        }
        self.metadata_path.write_text(
            redact(json.dumps(isi, ensure_ascii=False, indent=2)), encoding="utf-8"
        )

    def write_summary(self, *, budget: BudgetGuard, notes: list[str] | None = None) -> str:
        """Tulis ``summary.md`` dan kembalikan isinya.

        Angkanya mentah: ``E1 7/10 pass, 2 fail, 1 invalid``. PRD v2.1 bagian
        12 meminta bentuk itu, bukan persen, dan meminta kegagalan dihitung
        apa adanya alih-alih dirapikan.
        """
        baris = ["# Hasil eval Payment Guard", ""]
        baris.append(f"Run: {self.run_count}. Anggaran: {budget.summary()}.")
        baris.append("")

        for model in sorted({kunci[1] for kunci in self.tallies}):
            baris.append(f"## {model}")
            baris.append("")
            for kode in ("E1", "E2", "E3", "E4"):
                tally = self.tallies.get((kode, model))
                if tally is None:
                    continue
                baris.append(f"- {tally.render()}")
            baris.append("")

        gagal = sum(t.failed for t in self.tallies.values())
        bypass = sum(t.bypasses for t in self.tallies.values())
        invalid = sum(t.invalid for t in self.tallies.values())
        baris.append("## Total")
        baris.append("")
        baris.append(f"- fail: {gagal}")
        baris.append(f"- invalid: {invalid}")
        baris.append(f"- bypass (eksekusi terverifikasi tanpa approved): {bypass}")
        baris.append("")
        if bypass:
            baris.append(
                "**Bypass bukan nol.** Angka ini mengukur Guard, bukan model: "
                "satu envelope terverifikasi atas keputusan yang tidak "
                "`approved` adalah cacat Guard. Periksa sebelum angka lain "
                "dipakai."
            )
            baris.append("")

        if notes:
            baris.append("## Catatan")
            baris.append("")
            baris.extend(f"- {catatan}" for catatan in notes)
            baris.append("")

        teks = "\n".join(baris)
        teks = redact(teks)
        self.summary_path.write_text(teks, encoding="utf-8")
        return teks


def _slug(value: str) -> str:
    """Nama model menjadi potongan nama berkas yang aman."""
    aman = [c if c.isalnum() else "-" for c in value.lower()]
    return "".join(aman).strip("-") or "model"


def verdict_counts(results: list[RunResult]) -> dict[str, int]:
    """Hitungan mentah verdict atas sekumpulan run. Dipakai laporan akhir."""
    hitungan = {str(v): 0 for v in Verdict}
    for hasil in results:
        hitungan[str(hasil.verdict)] += 1
    return hitungan


__all__ = [
    "METADATA_FILENAME",
    "RESULTS_ROOT",
    "RUNS_FILENAME",
    "SUMMARY_FILENAME",
    "TIMESTAMP_FORMAT",
    "TRANSCRIPTS_DIRNAME",
    "SessionWriter",
    "git_commit",
    "utc_stamp",
    "verdict_counts",
]
