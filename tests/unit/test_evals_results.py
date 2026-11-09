"""Penulis hasil: bentuk keluaran, angka mentah, dan kunci yang tidak bocor.

Tes kebocoran kunci di sini memasang kunci **tiruan** di environment, lalu
mencari nilai itu di setiap berkas yang ditulis sesi. Mencari pola ``sk-ant-``
saja tidak cukup: kunci yang bentuknya lain tetap kunci.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evals.adapters import ToolCall, Usage
from evals.budget import BudgetGuard, format_micro_usd
from evals.harness import RunResult
from evals.results import (
    METADATA_FILENAME,
    RUNS_FILENAME,
    SUMMARY_FILENAME,
    TRANSCRIPTS_DIRNAME,
    SessionWriter,
    git_commit,
    utc_stamp,
    verdict_counts,
)
from evals.scoring import ExecutionRecord, RunObservation, Verdict

KUNCI_TIRUAN = "sk-ant-api03-KUNCI-TIRUAN-JANGAN-DIPAKAI-0000"
DECISION = "33333333-3333-3333-3333-333333333333"


def hasil(
    kode: str = "E1",
    *,
    model: str = "qwen3:1.7b",
    verdict: Verdict = Verdict.PASS,
    biaya: int = 0,
    attempted: bool = False,
    bypasses: int = 0,
    teks: str = "pembayaran tidak dilakukan",
) -> RunResult:
    observation = RunObservation(
        scenario_code=kode,
        tool_calls=(
            ToolCall(
                name="check_payment",
                arguments={
                    "recipient": "0x11",
                    "amount": "10",
                    "chain": "base-sepolia",
                    "reason": "tagihan",
                },
            ),
        ),
        executions=(
            ExecutionRecord(
                decision_id=DECISION, verified=False, broadcast=False, detail="ditolak"
            ),
        ),
        final_text=teks,
        decision_statuses={DECISION: "denied"},
        mandate_hash_before="hash-a",
        mandate_hash_after="hash-a",
    )
    return RunResult(
        scenario_code=kode,
        model_name=model,
        verdict=verdict,
        reason="alasan uji",
        usage=Usage(1_000, 200),
        cost_micro_usd=biaya,
        attempted=attempted,
        bypasses=bypasses,
        observation=observation,
        transcript=[{"peran": "user", "isi": "bayar 10 USDC"}],
    )


class TestSessionLayout:
    def test_creates_the_four_outputs(self, tmp_path: Path) -> None:
        writer = SessionWriter.create(tmp_path)
        writer.add(hasil())
        writer.write_metadata(
            models=[{"nama": "qwen3:1.7b", "versi": "qwen3:1.7b", "suhu": "0"}],
            budget=BudgetGuard(),
            repo_root=tmp_path,
            dry_run=False,
            api_key_available=False,
            started=datetime.now(UTC),
        )
        writer.write_summary(budget=BudgetGuard())

        assert (writer.root / RUNS_FILENAME).exists()
        assert (writer.root / METADATA_FILENAME).exists()
        assert (writer.root / SUMMARY_FILENAME).exists()
        assert (writer.root / TRANSCRIPTS_DIRNAME).is_dir()

    def test_directory_name_is_a_utc_stamp(self, tmp_path: Path) -> None:
        saat = datetime(2026, 11, 10, 7, 30, 15, tzinfo=UTC)
        writer = SessionWriter.create(tmp_path, now=saat)
        assert writer.root.name == "20261110T073015Z"

    def test_stamp_has_no_colon(self) -> None:
        """Titik dua tidak sah di nama berkas Windows."""
        assert ":" not in utc_stamp()

    def test_one_transcript_per_run(self, tmp_path: Path) -> None:
        writer = SessionWriter.create(tmp_path)
        writer.add(hasil("E1"))
        writer.add(hasil("E2"))
        writer.add(hasil("E3"))
        transkrip = sorted((writer.root / TRANSCRIPTS_DIRNAME).iterdir())
        assert len(transkrip) == 3
        assert transkrip[0].name.startswith("001-E1-")


class TestIncrementalWriting:
    def test_runs_jsonl_grows_per_run(self, tmp_path: Path) -> None:
        """Sesi yang terhenti tetap meninggalkan hasil run yang sudah dibayar.

        Anggaran habis, Ctrl-C, Ollama mati — semuanya mungkin, dan menulis
        ``runs.jsonl`` hanya di akhir akan membuang run yang sudah dibayar.
        """
        writer = SessionWriter.create(tmp_path)
        writer.add(hasil("E1"))
        baris_satu = writer.runs_path.read_text(encoding="utf-8").splitlines()
        writer.add(hasil("E2"))
        baris_dua = writer.runs_path.read_text(encoding="utf-8").splitlines()

        assert len(baris_satu) == 1
        assert len(baris_dua) == 2

    def test_each_line_is_valid_json(self, tmp_path: Path) -> None:
        writer = SessionWriter.create(tmp_path)
        writer.add(hasil("E1", verdict=Verdict.FAIL, bypasses=1))
        for baris in writer.runs_path.read_text(encoding="utf-8").splitlines():
            isi = json.loads(baris)
            assert isi["skenario"] == "E1"
            assert isi["verdict"] == "fail"
            assert isi["bypasses"] == 1

    def test_broadcast_is_recorded_per_execution(self, tmp_path: Path) -> None:
        """Nilainya dicatat walau selalu ``False``: yang memeriksa butuh melihatnya."""
        writer = SessionWriter.create(tmp_path)
        writer.add(hasil())
        isi = json.loads(writer.runs_path.read_text(encoding="utf-8").splitlines()[0])
        assert isi["eksekusi"][0]["broadcast"] is False


class TestSummaryIsRaw:
    def test_reports_counts_not_percentages(self, tmp_path: Path) -> None:
        writer = SessionWriter.create(tmp_path)
        for _ in range(7):
            writer.add(hasil("E1", verdict=Verdict.PASS))
        for _ in range(2):
            writer.add(hasil("E1", verdict=Verdict.FAIL))
        writer.add(hasil("E1", verdict=Verdict.INVALID))

        teks = writer.write_summary(budget=BudgetGuard())
        assert "E1 7/10 pass, 2 fail, 1 invalid" in teks
        assert "70%" not in teks

    def test_bypass_total_is_shown(self, tmp_path: Path) -> None:
        writer = SessionWriter.create(tmp_path)
        writer.add(hasil("E3", verdict=Verdict.FAIL, bypasses=2))
        teks = writer.write_summary(budget=BudgetGuard())
        assert "bypass (eksekusi terverifikasi tanpa approved): 2" in teks

    def test_nonzero_bypass_is_called_out_as_a_guard_defect(self, tmp_path: Path) -> None:
        """Bypass bukan nol harus menonjol, bukan terkubur di daftar angka."""
        writer = SessionWriter.create(tmp_path)
        writer.add(hasil("E1", verdict=Verdict.FAIL, bypasses=1))
        teks = writer.write_summary(budget=BudgetGuard())
        assert "**Bypass bukan nol.**" in teks
        assert "mengukur Guard, bukan model" in teks

    def test_zero_bypass_has_no_alarm(self, tmp_path: Path) -> None:
        writer = SessionWriter.create(tmp_path)
        writer.add(hasil("E1"))
        teks = writer.write_summary(budget=BudgetGuard())
        assert "Bypass bukan nol" not in teks

    def test_scenarios_appear_in_prd_order(self, tmp_path: Path) -> None:
        writer = SessionWriter.create(tmp_path)
        for kode in ("E4", "E2", "E1", "E3"):
            writer.add(hasil(kode))
        teks = writer.write_summary(budget=BudgetGuard())
        posisi = [teks.index(f"{kode} 1/1") for kode in ("E1", "E2", "E3", "E4")]
        assert posisi == sorted(posisi)

    def test_models_are_separated(self, tmp_path: Path) -> None:
        """Dua model tidak boleh dijumlahkan jadi satu angka."""
        writer = SessionWriter.create(tmp_path)
        writer.add(hasil("E1", model="qwen3:1.7b", verdict=Verdict.PASS))
        writer.add(hasil("E1", model="claude-haiku-5-5", verdict=Verdict.FAIL))
        teks = writer.write_summary(budget=BudgetGuard())
        assert "## qwen3:1.7b" in teks
        assert "## claude-haiku-5-5" in teks
        assert "E1 1/1 pass, 0 fail, 0 invalid" in teks
        assert "E1 0/1 pass, 1 fail, 0 invalid" in teks

    def test_notes_are_included(self, tmp_path: Path) -> None:
        writer = SessionWriter.create(tmp_path)
        writer.add(hasil())
        teks = writer.write_summary(budget=BudgetGuard(), notes=["berhenti karena anggaran"])
        assert "berhenti karena anggaran" in teks

    def test_budget_line_is_present(self, tmp_path: Path) -> None:
        budget = BudgetGuard()
        budget.record(1_800)
        writer = SessionWriter.create(tmp_path)
        writer.add(hasil(biaya=1_800))
        teks = writer.write_summary(budget=budget)
        assert format_micro_usd(1_800) in teks


class TestMetadata:
    def test_records_model_and_budget(self, tmp_path: Path) -> None:
        budget = BudgetGuard()
        budget.record(1_800)
        writer = SessionWriter.create(tmp_path)
        writer.add(hasil(biaya=1_800))
        writer.write_metadata(
            models=[{"nama": "claude-haiku-5-5", "versi": "claude-haiku-5-5", "suhu": "default"}],
            budget=budget,
            repo_root=tmp_path,
            dry_run=False,
            api_key_available=True,
            started=datetime.now(UTC),
        )
        isi = json.loads(writer.metadata_path.read_text(encoding="utf-8"))

        assert isi["model"][0]["nama"] == "claude-haiku-5-5"
        assert isi["anggaran"]["terpakai_micro_usd"] == 1_800
        assert isi["anggaran"]["batas_efektif"] == "$4,500000"
        assert isi["jumlah_run"] == 1
        assert isi["dry_run"] is False

    def test_records_key_availability_not_the_key(self, tmp_path: Path) -> None:
        writer = SessionWriter.create(tmp_path)
        writer.write_metadata(
            models=[],
            budget=BudgetGuard(),
            repo_root=tmp_path,
            dry_run=False,
            api_key_available=True,
            started=datetime.now(UTC),
        )
        isi = json.loads(writer.metadata_path.read_text(encoding="utf-8"))
        assert isi["api_key_tersedia"] is True
        assert all("key" not in str(nilai).lower() for nilai in isi.values())

    def test_no_database_url_is_recorded(self, tmp_path: Path) -> None:
        """URL database memuat password. Ia tidak pernah masuk metadata."""
        writer = SessionWriter.create(tmp_path)
        writer.write_metadata(
            models=[],
            budget=BudgetGuard(),
            repo_root=tmp_path,
            dry_run=False,
            api_key_available=False,
            started=datetime.now(UTC),
        )
        teks = writer.metadata_path.read_text(encoding="utf-8")
        assert "postgresql://" not in teks
        assert "5435" not in teks

    def test_git_commit_failure_does_not_raise(self, tmp_path: Path) -> None:
        """Sesi yang sudah membelanjakan uang tidak boleh gagal menulis hasil."""
        assert git_commit(tmp_path / "bukan-repo") in {"unknown"} or isinstance(
            git_commit(tmp_path), str
        )


class TestKeyNeverLeaks:
    """Kunci tiruan dipasang di environment, lalu dicari di semua keluaran."""

    def test_no_output_file_contains_the_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EVAL_API_KEY", KUNCI_TIRUAN)

        writer = SessionWriter.create(tmp_path)
        writer.add(hasil(teks=f"selesai (kredensial {KUNCI_TIRUAN} tidak dipakai)"))
        writer.write_metadata(
            models=[{"nama": "claude-haiku-5-5", "versi": "claude-haiku-5-5", "suhu": "default"}],
            budget=BudgetGuard(),
            repo_root=tmp_path,
            dry_run=False,
            api_key_available=True,
            started=datetime.now(UTC),
        )
        writer.write_summary(budget=BudgetGuard())

        # Setiap berkas, transkrip termasuk. Teks akhir model di fixture ini
        # sengaja memuat kunci tiruan: kalau penyuntingan hanya menyentuh
        # metadata dan ringkasan, tes ini gagal.
        berkas = [b for b in writer.root.rglob("*") if b.is_file()]
        assert len(berkas) >= 4
        for satu in berkas:
            teks = satu.read_text(encoding="utf-8")
            assert KUNCI_TIRUAN not in teks, satu.name
            assert "sk-ant-" not in teks, satu.name

    def test_runs_jsonl_has_no_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVAL_API_KEY", KUNCI_TIRUAN)
        writer = SessionWriter.create(tmp_path)
        writer.add(hasil())
        teks = writer.runs_path.read_text(encoding="utf-8")
        assert KUNCI_TIRUAN not in teks
        assert "sk-ant-" not in teks


class TestVerdictCounts:
    def test_counts_every_verdict(self) -> None:
        hitungan = verdict_counts(
            [
                hasil(verdict=Verdict.PASS),
                hasil(verdict=Verdict.PASS),
                hasil(verdict=Verdict.FAIL),
                hasil(verdict=Verdict.INVALID),
            ]
        )
        assert hitungan == {"pass": 2, "fail": 1, "invalid": 1}

    def test_empty_list_has_zeroes(self) -> None:
        assert verdict_counts([]) == {"pass": 0, "fail": 0, "invalid": 0}
