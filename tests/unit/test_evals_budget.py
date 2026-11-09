"""Anggaran: aritmetika bilangan bulat, dan berhenti **sebelum** melewati batas.

Yang diuji di sini bukan "apakah angkanya benar" saja, tapi urutannya: penjaga
harus menolak run **sebelum** permintaan dikirim. Penjaga yang mencatat
pelanggaran sesudah uangnya terpakai bukan penjaga, ia catatan.
"""

from __future__ import annotations

import pytest

from evals.budget import (
    DEFAULT_BUDGET_MICRO_USD,
    FIRST_RUN_ESTIMATE_MICRO_USD,
    MICRO_USD_PER_USD,
    RESERVE_PERCENT,
    BudgetExceeded,
    BudgetGuard,
    format_micro_usd,
    usd_to_micro,
)


class TestUsdToMicro:
    def test_five_usd(self) -> None:
        assert usd_to_micro(5) == 5_000_000
        assert DEFAULT_BUDGET_MICRO_USD == 5_000_000

    def test_rejects_float(self) -> None:
        """``--budget-usd 2.5`` ditolak dengan sengaja.

        Menerimanya berarti membawa ``float`` ke jalur uang. Anggaran yang
        meleset karena pembulatan biner tidak bisa dipertanggungjawabkan ke
        siapa pun, dan 2,5 USD bukan kebutuhan yang nyata di sini.
        """
        with pytest.raises(ValueError, match="bilangan bulat"):
            usd_to_micro(2.5)  # type: ignore[arg-type]

    def test_rejects_bool(self) -> None:
        """``True`` adalah ``int`` di Python. Ia bukan anggaran."""
        with pytest.raises(ValueError, match="bilangan bulat"):
            usd_to_micro(True)  # type: ignore[arg-type]

    @pytest.mark.parametrize("nilai", [0, -1, -100])
    def test_rejects_non_positive(self, nilai: int) -> None:
        with pytest.raises(ValueError, match="lebih besar dari nol"):
            usd_to_micro(nilai)


class TestFormatting:
    @pytest.mark.parametrize(
        ("micro", "teks"),
        [
            (0, "$0,000000"),
            (1, "$0,000001"),
            (1_800, "$0,001800"),
            (5_000_000, "$5,000000"),
            (4_500_000, "$4,500000"),
            (-1_800, "-$0,001800"),
        ],
    )
    def test_renders_with_comma(self, micro: int, teks: str) -> None:
        """Pemisah desimalnya koma: seluruh laporan berbahasa Indonesia."""
        assert format_micro_usd(micro) == teks

    def test_never_loses_precision(self) -> None:
        """Enam angka pecahan selalu, jadi 1 micro-USD tidak pernah jadi $0,00."""
        assert format_micro_usd(1) != format_micro_usd(0)


class TestEffectiveLimit:
    def test_reserve_is_withheld(self) -> None:
        guard = BudgetGuard()
        assert guard.limit_micro_usd == 5_000_000
        assert guard.reserve_percent == RESERVE_PERCENT
        assert guard.reserve_micro_usd == 500_000
        assert guard.usable_micro_usd == 4_500_000

    def test_remaining_never_negative(self) -> None:
        guard = BudgetGuard(limit_micro_usd=1_000_000)
        guard.record(999_999_999)
        assert guard.remaining_micro_usd == 0

    def test_rejects_non_positive_limit(self) -> None:
        with pytest.raises(ValueError, match="positif"):
            BudgetGuard(limit_micro_usd=0)

    @pytest.mark.parametrize("persen", [-1, 100, 150])
    def test_rejects_impossible_reserve(self, persen: int) -> None:
        with pytest.raises(ValueError, match="cadangan"):
            BudgetGuard(reserve_percent=persen)

    def test_zero_reserve_is_allowed(self) -> None:
        """Nol cadangan sah: operator boleh memakai seluruh batas."""
        guard = BudgetGuard(limit_micro_usd=1_000_000, reserve_percent=0)
        assert guard.usable_micro_usd == 1_000_000


class TestProjection:
    def test_first_run_uses_the_estimate(self) -> None:
        assert BudgetGuard().projected_next_run() == FIRST_RUN_ESTIMATE_MICRO_USD

    def test_later_runs_use_the_most_expensive_so_far(self) -> None:
        """Run termahal, bukan rata-rata.

        Rata-rata akan meremehkan run panjang berikutnya, dan run yang tidak
        biasa itu justru yang bisa melewati batas.
        """
        guard = BudgetGuard()
        guard.record(1_000)
        guard.record(9_000)
        guard.record(2_000)
        assert guard.projected_next_run() == 9_000


class TestStopsBeforeSpending:
    """Inti modul ini: penolakan terjadi sebelum uang keluar."""

    def test_allows_run_well_within_budget(self) -> None:
        BudgetGuard().check_before_run()

    def test_refuses_when_projection_would_exceed(self) -> None:
        guard = BudgetGuard(limit_micro_usd=10_000, reserve_percent=0)
        guard.record(6_000)
        assert guard.projected_next_run() == 6_000
        assert guard.would_exceed() is True
        with pytest.raises(BudgetExceeded):
            guard.check_before_run()

    def test_refusal_leaves_spending_unchanged(self) -> None:
        """Penolakan tidak menambah pengeluaran. Uangnya memang belum terpakai."""
        guard = BudgetGuard(limit_micro_usd=10_000, reserve_percent=0)
        guard.record(6_000)
        sebelum = guard.spent_micro_usd
        with pytest.raises(BudgetExceeded):
            guard.check_before_run()
        assert guard.spent_micro_usd == sebelum
        assert guard.runs == 1

    def test_reserve_makes_the_guard_stop_earlier(self) -> None:
        """Dengan cadangan, run terakhir ditolak lebih awal — itu tujuannya.

        Batas 10.000 dengan cadangan 10% berarti 9.000 yang boleh dipakai.
        Setelah 5.000 terpakai dan proyeksi 5.000, tanpa cadangan ia masih
        boleh jalan (10.000 tepat di batas); dengan cadangan ia ditolak.
        """
        ketat = BudgetGuard(limit_micro_usd=10_000, reserve_percent=10)
        ketat.record(5_000)
        assert ketat.would_exceed() is True

        longgar = BudgetGuard(limit_micro_usd=10_000, reserve_percent=0)
        longgar.record(5_000)
        assert longgar.would_exceed() is False

    def test_refusal_message_has_the_numbers(self) -> None:
        """Operator harus bisa memeriksa keputusannya, bukan hanya membacanya."""
        guard = BudgetGuard(limit_micro_usd=10_000, reserve_percent=0)
        guard.record(9_999)
        with pytest.raises(BudgetExceeded) as info:
            guard.check_before_run()
        pesan = str(info.value)
        assert format_micro_usd(9_999) in pesan
        assert format_micro_usd(10_000) in pesan
        assert "1 run" in pesan

    def test_exact_fit_is_allowed(self) -> None:
        """Proyeksi yang tepat sama dengan sisa boleh jalan. Batasnya ``>``."""
        guard = BudgetGuard(limit_micro_usd=10_000, reserve_percent=0)
        guard.record(5_000)
        assert guard.projected_next_run() == 5_000
        guard.check_before_run()


class TestRecording:
    def test_counts_runs_and_sums_costs(self) -> None:
        guard = BudgetGuard()
        guard.record(100)
        guard.record(250)
        assert guard.runs == 2
        assert guard.spent_micro_usd == 350
        assert guard.costs == [100, 250]

    def test_zero_cost_run_still_counts(self) -> None:
        """Run Ollama gratis, tapi ia tetap satu run."""
        guard = BudgetGuard()
        guard.record(0)
        assert guard.runs == 1
        assert guard.spent_micro_usd == 0

    def test_rejects_negative_cost(self) -> None:
        with pytest.raises(ValueError, match="negatif"):
            BudgetGuard().record(-1)

    def test_summary_is_one_line(self) -> None:
        guard = BudgetGuard()
        guard.record(1_800)
        ringkasan = guard.summary()
        assert "\n" not in ringkasan
        assert "1 run" in ringkasan
        assert format_micro_usd(1_800) in ringkasan


class TestFreeArmNeverStops:
    """Arm Ollama tidak berbiaya, jadi anggaran tidak boleh pernah menghentikannya."""

    def test_forty_free_runs(self) -> None:
        guard = BudgetGuard()
        for _ in range(40):
            guard.check_before_run()
            guard.record(0)
        assert guard.runs == 40
        assert guard.spent_micro_usd == 0


def test_micro_per_usd_is_a_million() -> None:
    assert MICRO_USD_PER_USD == 1_000_000
