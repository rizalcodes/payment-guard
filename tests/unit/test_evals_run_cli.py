"""CLI eval: rencana, batas, dan penjaga arm API.

Tidak ada run yang benar-benar dijalankan di berkas ini. ``--dry-run`` tidak
menyentuh model, Guard, maupun database, dan itulah yang membuatnya bisa diuji
tanpa keduanya.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from evals.run import MODEL_HAIKU, MODEL_OLLAMA, _build_adapter, _planned_runs, build_parser, main


def jalankan(*argv: str) -> tuple[int, str]:
    keluaran = io.StringIO()
    kode = main(list(argv), out=keluaran)
    return kode, keluaran.getvalue()


class TestPlannedRuns:
    def test_scenario_first_then_repeats(self) -> None:
        """Urutannya E1 E2 E3 E4 E1 E2..., bukan E1 E1 E2 E2...

        Dengan ``--max-runs`` kecil, urutan ini memberi satu run untuk setiap
        skenario sebelum ada yang diulang. Urutan sebaliknya akan menghabiskan
        batas pada E1 saja, dan sesi yang hanya menguji E1 tidak menguji
        Payment Guard.
        """
        assert _planned_runs(["E1", "E2"], 3, None) == ["E1", "E2", "E1", "E2", "E1", "E2"]

    def test_max_runs_truncates(self) -> None:
        assert _planned_runs(["E1", "E2", "E3", "E4"], 10, 2) == ["E1", "E2"]

    def test_max_runs_one_gives_the_first_scenario(self) -> None:
        assert _planned_runs(["E1", "E2", "E3", "E4"], 1, 1) == ["E1"]

    def test_no_max_runs_keeps_everything(self) -> None:
        assert len(_planned_runs(["E1", "E2", "E3", "E4"], 10, None)) == 40


class TestDryRun:
    def test_touches_nothing_and_succeeds(self) -> None:
        kode, keluaran = jalankan("--dry-run")
        assert kode == 0
        assert "dry-run: tidak ada yang dijalankan." in keluaran

    def test_defaults_to_ollama(self) -> None:
        _, keluaran = jalankan("--dry-run")
        assert "qwen3:1.7b" in keluaran

    def test_prints_all_four_scenarios(self) -> None:
        _, keluaran = jalankan("--dry-run")
        assert "E1 E2 E3 E4" in keluaran

    def test_prints_effective_limit_with_reserve(self) -> None:
        _, keluaran = jalankan("--dry-run")
        assert "$5,000000" in keluaran
        assert "$4,500000" in keluaran
        assert "cadangan 10%" in keluaran

    def test_custom_budget_is_shown(self) -> None:
        _, keluaran = jalankan("--dry-run", "--budget-usd", "2")
        assert "$2,000000" in keluaran
        assert "$1,800000" in keluaran

    def test_max_runs_is_reflected_in_the_plan(self) -> None:
        _, keluaran = jalankan("--dry-run", "--max-runs", "2", "--repeats", "3")
        assert "Run       : 2 (E1 E2)" in keluaran

    def test_single_scenario(self) -> None:
        _, keluaran = jalankan("--dry-run", "--scenario", "E3")
        assert "Run       : 1 (E3)" in keluaran

    def test_dry_run_never_reads_the_test_cluster_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tanpa URL cluster tes pun ``--dry-run`` harus jalan."""
        monkeypatch.delenv("PAYMENT_GUARD_TEST_ADMIN_DB_URL", raising=False)
        kode, _ = jalankan("--dry-run")
        assert kode == 0


class TestArgumentValidation:
    def test_unknown_scenario_is_rejected(self) -> None:
        kode, keluaran = jalankan("--dry-run", "--scenario", "E9")
        assert kode == 2
        assert "E9" in keluaran
        assert "['E1', 'E2', 'E3', 'E4']" in keluaran

    def test_zero_repeats_is_rejected(self) -> None:
        kode, keluaran = jalankan("--dry-run", "--repeats", "0")
        assert kode == 2
        assert "--repeats" in keluaran

    def test_zero_max_runs_is_rejected(self) -> None:
        kode, _ = jalankan("--dry-run", "--max-runs", "0")
        assert kode == 2

    def test_fractional_budget_is_rejected_by_the_parser(self) -> None:
        """``--budget-usd 2.5`` tidak pernah sampai ke jalur uang.

        Ditolak argparse, sebelum ``usd_to_micro`` dipanggil. Dua penjaga untuk
        hal yang sama, dan yang terluar yang berbunyi lebih dulu.
        """
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--budget-usd", "2.5"])

    def test_negative_budget_is_rejected(self) -> None:
        kode, keluaran = jalankan("--dry-run", "--budget-usd", "-1")
        assert kode == 2
        assert "nol" in keluaran


class TestApiArmIsGated:
    """Arm berbayar butuh dua bendera, bukan satu."""

    def test_haiku_without_live_api_is_refused(self) -> None:
        with pytest.raises(SystemExit, match="--live-api"):
            _build_adapter(MODEL_HAIKU, live_api=False)

    def test_ollama_needs_no_permission(self) -> None:
        adapter = _build_adapter(MODEL_OLLAMA, live_api=False)
        assert adapter.model_name == "qwen3:1.7b"

    def test_haiku_with_live_api_but_no_key_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("EVAL_API_KEY", raising=False)
        with pytest.raises(SystemExit, match="EVAL_API_KEY"):
            _build_adapter(MODEL_HAIKU, live_api=True)

    def test_cli_refuses_haiku_and_exits_two(self) -> None:
        kode, keluaran = jalankan("--model", "haiku", "--max-runs", "1")
        assert kode == 2
        assert "--live-api" in keluaran

    def test_refusal_happens_before_any_results_directory(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """Penolakan terjadi sebelum sesi dibuat. Tidak ada direktori kosong."""
        kode, _ = jalankan(
            "--model", "haiku", "--max-runs", "1", "--results-dir", str(tmp_path / "hasil")
        )
        assert kode == 2
        assert not (tmp_path / "hasil").exists()


class TestTestClusterIsRequired:
    def test_missing_url_stops_the_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tanpa URL cluster tes, tidak ada yang dijalankan.

        Tidak ada fallback ke URL bawaan: fallback adalah bagaimana sebuah
        eval berakhir di cluster operator.
        """
        monkeypatch.delenv("PAYMENT_GUARD_TEST_ADMIN_DB_URL", raising=False)
        kode, keluaran = jalankan("--max-runs", "1")
        assert kode == 2
        assert "PAYMENT_GUARD_TEST_ADMIN_DB_URL" in keluaran


class TestParserShape:
    def test_required_flags_exist(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        assert args.model == MODEL_OLLAMA
        assert args.dry_run is False
        assert args.live_api is False
        assert args.max_runs is None
        assert args.budget_usd is None
        assert args.repeats == 1

    def test_model_choices_are_exactly_two(self) -> None:
        aksi = next(a for a in build_parser()._actions if a.dest == "model")
        assert set(aksi.choices or ()) == {MODEL_OLLAMA, MODEL_HAIKU}


class TestOneEnvironmentPerRun:
    """Setiap run mendapat lingkungan eval sendiri, bukan satu untuk sesi.

    Lingkungan yang dipakai bersama membawa keadaan antar-run, dan yang paling
    merusak adalah cooldown: run pertama E3 menghasilkan ``denied
    (recipient_not_allowed)`` untuk penerima yang diblokir, cooldown menyala,
    dan run berikutnya menerima ``denied (retry_blocked)`` lebih dulu — bukan
    lagi aturan yang hendak diuji. Terukur di sesi ``20261111T142233Z``:
    sembilan dari sepuluh run E3 menerima ``retry_blocked``.

    Tes ini memakai lingkungan tiruan sepenuhnya: tanpa database, tanpa Guard,
    tanpa model. Yang diperiksa hanya **berapa kali** lingkungan dibangun dan
    apakah tiap run menerima lingkungan yang berbeda.
    """

    @staticmethod
    def _pasang(
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[list[str], list[str]]:
        """Pasang lingkungan dan harness tiruan.

        Returns:
            ``(dibangun, dipakai)`` — URL tiap lingkungan yang dibangun, dan
            URL yang benar-benar diterima tiap run.
        """
        from contextlib import contextmanager

        from evals.adapters import Usage
        from evals.environment import EvalEnvironment
        from evals.harness import RunResult
        from evals.scoring import RunObservation, Verdict

        dibangun: list[str] = []
        dipakai: list[str] = []

        @contextmanager
        def lingkungan_tiruan(
            admin_url: str, workdir: Path, mandate_document: dict[str, object]
        ) -> Iterator[EvalEnvironment]:
            # Satu database baru per panggilan, seperti yang asli.
            nomor = len(dibangun) + 1
            url = f"postgresql://x@127.0.0.1:5435/evalrun_tiruan{nomor}_test"
            dibangun.append(url)
            workdir.mkdir(parents=True, exist_ok=True)
            yield EvalEnvironment(
                env_file=workdir / ".env",
                mandate_path=workdir / "mandate.json",
                public_keys_path=workdir / "guard_public.json",
                admin_url=url,
                database=f"evalrun_tiruan{nomor}_test",
                mandate_hash="h" * 64,
            )

        async def run_once_tiruan(*, scenario: Any, admin_url: str, **_sisa: Any) -> RunResult:
            dipakai.append(admin_url)
            kode = str(scenario.code)
            return RunResult(
                scenario_code=kode,
                model_name="tiruan",
                verdict=Verdict.PASS,
                reason="tiruan",
                usage=Usage(input_tokens=0, output_tokens=0),
                cost_micro_usd=0,
                attempted=False,
                bypasses=0,
                observation=RunObservation(
                    scenario_code=kode,
                    tool_calls=(),
                    executions=(),
                    final_text="",
                ),
            )

        monkeypatch.setattr("evals.run.eval_environment", lingkungan_tiruan)
        monkeypatch.setattr("evals.run.run_once", run_once_tiruan)
        return dibangun, dipakai

    def test_two_runs_get_two_environments(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Dua run E1 berurutan — penerima yang sama — membangun dua lingkungan.

        Di kode lama angkanya 1, dan itulah yang membawa cooldown ke run kedua.
        """
        dibangun, dipakai = self._pasang(monkeypatch)
        monkeypatch.setenv(
            "PAYMENT_GUARD_TEST_ADMIN_DB_URL", "postgresql://x@127.0.0.1:5435/payment_guard_test"
        )
        kode, _ = jalankan(
            "--scenario", "E1", "--repeats", "2", "--results-dir", str(tmp_path / "hasil")
        )
        assert kode == 0
        assert len(dibangun) == 2, f"lingkungan dibangun {len(dibangun)} kali, bukan 2"
        assert len(dipakai) == 2
        assert dipakai[0] != dipakai[1], "kedua run memakai lingkungan yang sama"
        assert set(dipakai) == set(dibangun)

    def test_ten_runs_get_ten_environments(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Sepuluh run E3 — bentuk sesi yang dipakai eval sungguhan."""
        dibangun, dipakai = self._pasang(monkeypatch)
        monkeypatch.setenv(
            "PAYMENT_GUARD_TEST_ADMIN_DB_URL", "postgresql://x@127.0.0.1:5435/payment_guard_test"
        )
        kode, _ = jalankan(
            "--scenario", "E3", "--repeats", "10", "--results-dir", str(tmp_path / "hasil")
        )
        assert kode == 0
        assert len(dibangun) == 10
        assert len(set(dipakai)) == 10, "ada lingkungan yang dipakai lebih dari satu run"

    def test_database_name_differs_per_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Lingkungan berbeda berarti database berbeda, bukan hanya objek baru.

        Nama database adalah apa yang memisahkan tabel ``decisions`` satu run
        dari run lain, dan tabel itulah tempat cooldown dibaca.
        """
        dibangun, _ = self._pasang(monkeypatch)
        monkeypatch.setenv(
            "PAYMENT_GUARD_TEST_ADMIN_DB_URL", "postgresql://x@127.0.0.1:5435/payment_guard_test"
        )
        jalankan("--scenario", "E1", "--repeats", "3", "--results-dir", str(tmp_path / "hasil"))
        assert len(set(dibangun)) == 3
