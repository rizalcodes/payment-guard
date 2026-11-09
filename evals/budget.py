"""Penjaga anggaran: berhenti **sebelum** melewati batas, bukan sesudah.

PRD v2.1 bagian 12: "Budget keras 5 USD. Runner berhenti bila estimasi biaya
terlewati."

Seluruh modul ini memakai bilangan bulat **micro-USD**. Tidak ada ``float``,
dan itu bukan kemurnian: anggaran adalah uang, dan uang yang dihitung dengan
aritmetika biner akan meleset dengan cara yang tidak bisa dijelaskan ke siapa
pun. $5 adalah ``5_000_000``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

#: Micro-USD per USD.
MICRO_USD_PER_USD: Final = 1_000_000

#: Batas bawaan: 5 USD (PRD v2.1 bagian 12).
DEFAULT_BUDGET_MICRO_USD: Final = 5 * MICRO_USD_PER_USD

#: Cadangan yang tidak pernah dibelanjakan, dalam persen dari batas.
#:
#: Penjaga ini membandingkan biaya sekarang **ditambah proyeksi run
#: berikutnya** terhadap batas dikurangi cadangan. Cadangan itu menutup hal
#: yang proyeksinya tidak bisa tahu: satu run yang kebetulan jauh lebih
#: panjang dari rata-rata. Tanpa cadangan, run terakhir bisa melewati batas
#: justru karena ia run yang tidak biasa.
RESERVE_PERCENT: Final = 10

#: Proyeksi biaya run pertama, sebelum ada data nyata, dalam micro-USD.
#:
#: Dari perkiraan M8 Fase 1: ~6.000 token input dan ~2.400 token output di
#: Claude Haiku 5.5, yaitu sekitar 1.800 micro-USD. Dilebihkan menjadi 5.000
#: supaya run pertama tidak pernah menjadi run yang melewati batas. Setelah
#: satu run selesai, proyeksinya memakai angka nyata dan tebakan ini tidak
#: dipakai lagi.
FIRST_RUN_ESTIMATE_MICRO_USD: Final = 5_000


def usd_to_micro(usd: int) -> int:
    """USD bulat menjadi micro-USD. Hanya menerima bilangan bulat.

    Raises:
        ValueError: untuk ``float`` atau nilai negatif. ``--budget-usd 2.5``
            ditolak dengan sengaja: menerimanya berarti membawa ``float`` ke
            jalur uang, dan 2,5 USD bukan kebutuhan yang nyata di sini.
    """
    if isinstance(usd, bool) or not isinstance(usd, int):
        raise ValueError(f"anggaran harus bilangan bulat USD, bukan {type(usd).__name__}")
    if usd <= 0:
        raise ValueError(f"anggaran harus lebih besar dari nol, dapat {usd}")
    return usd * MICRO_USD_PER_USD


def format_micro_usd(micro: int) -> str:
    """``1800`` menjadi ``"$0,001800"``. Pembagian bilangan bulat saja.

    Pemisah desimalnya koma karena seluruh laporan berbahasa Indonesia.
    """
    if micro < 0:
        return "-" + format_micro_usd(-micro)
    utuh, pecahan = divmod(micro, MICRO_USD_PER_USD)
    return f"${utuh},{pecahan:06d}"


class BudgetExceeded(RuntimeError):
    """Anggaran akan terlewati bila run berikutnya dijalankan.

    Dilempar **sebelum** permintaan dikirim, jadi uangnya belum terpakai.
    """


@dataclass
class BudgetGuard:
    """Melacak biaya dan menolak run yang akan melewati batas.

    Pemakaiannya satu urutan tetap per run::

        guard.check_before_run()      # melempar bila proyeksinya melewati batas
        ...                           # jalankan run
        guard.record(biaya_run_itu)   # catat biaya nyatanya

    Proyeksi run berikutnya adalah biaya **run termahal sejauh ini**, bukan
    rata-ratanya. Rata-rata akan meremehkan run panjang berikutnya, dan yang
    ingin dihindari justru run yang tidak biasa.
    """

    limit_micro_usd: int = DEFAULT_BUDGET_MICRO_USD
    reserve_percent: int = RESERVE_PERCENT
    spent_micro_usd: int = 0
    runs: int = 0
    #: Biaya tiap run yang sudah selesai, untuk laporan dan untuk proyeksi.
    costs: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.limit_micro_usd <= 0:
            raise ValueError(f"batas harus positif, dapat {self.limit_micro_usd}")
        if not 0 <= self.reserve_percent < 100:
            raise ValueError(f"cadangan harus 0..99 persen, dapat {self.reserve_percent}")

    @property
    def reserve_micro_usd(self) -> int:
        """Bagian batas yang tidak pernah dibelanjakan. Pembagian bulat."""
        return self.limit_micro_usd * self.reserve_percent // 100

    @property
    def usable_micro_usd(self) -> int:
        """Batas efektif: batas dikurangi cadangan."""
        return self.limit_micro_usd - self.reserve_micro_usd

    @property
    def remaining_micro_usd(self) -> int:
        """Sisa di dalam batas efektif. Tidak pernah negatif."""
        return max(0, self.usable_micro_usd - self.spent_micro_usd)

    def projected_next_run(self) -> int:
        """Perkiraan biaya run berikutnya.

        Run termahal sejauh ini, atau :data:`FIRST_RUN_ESTIMATE_MICRO_USD`
        sebelum ada run yang selesai.
        """
        return max(self.costs) if self.costs else FIRST_RUN_ESTIMATE_MICRO_USD

    def would_exceed(self) -> bool:
        """Apakah run berikutnya akan melewati batas efektif."""
        return self.spent_micro_usd + self.projected_next_run() > self.usable_micro_usd

    def check_before_run(self) -> None:
        """Lempar bila run berikutnya akan melewati batas.

        Raises:
            BudgetExceeded: dengan angka yang bisa diperiksa operator.
        """
        if self.would_exceed():
            raise BudgetExceeded(
                "berhenti sebelum melewati anggaran: sudah terpakai "
                f"{format_micro_usd(self.spent_micro_usd)} dari batas efektif "
                f"{format_micro_usd(self.usable_micro_usd)} "
                f"(batas {format_micro_usd(self.limit_micro_usd)} dikurangi cadangan "
                f"{self.reserve_percent}%), dan run berikutnya diperkirakan "
                f"{format_micro_usd(self.projected_next_run())}. "
                f"{self.runs} run sudah selesai."
            )

    def record(self, cost_micro_usd: int) -> None:
        """Catat biaya satu run yang sudah selesai.

        Raises:
            ValueError: untuk biaya negatif.
        """
        if cost_micro_usd < 0:
            raise ValueError(f"biaya tidak boleh negatif, dapat {cost_micro_usd}")
        self.spent_micro_usd += cost_micro_usd
        self.costs.append(cost_micro_usd)
        self.runs += 1

    def summary(self) -> str:
        """Satu baris untuk laporan akhir."""
        return (
            f"{self.runs} run, {format_micro_usd(self.spent_micro_usd)} dari "
            f"{format_micro_usd(self.limit_micro_usd)} "
            f"(sisa {format_micro_usd(self.remaining_micro_usd)} di dalam batas efektif)"
        )


__all__ = [
    "DEFAULT_BUDGET_MICRO_USD",
    "FIRST_RUN_ESTIMATE_MICRO_USD",
    "MICRO_USD_PER_USD",
    "RESERVE_PERCENT",
    "BudgetExceeded",
    "BudgetGuard",
    "format_micro_usd",
    "usd_to_micro",
]
