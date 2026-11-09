"""CLI eval: rencana, batas, dan penjaga arm API.

Tidak ada run yang benar-benar dijalankan di berkas ini. ``--dry-run`` tidak
menyentuh model, Guard, maupun database, dan itulah yang membuatnya bisa diuji
tanpa keduanya.
"""

from __future__ import annotations

import io

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
