"""Chain yang didukung, dan pemetaannya ke ``chain_id``.

PRD v2 memakai dua representasi untuk chain yang sama, dan keduanya disengaja:

``chain``
    String di mandate dan di argumen tool ``check_payment`` (PRD v2 bagian 6.1
    dan 7). PRD v2 bagian 6.2: "``chain`` hanya ``base-sepolia``".

``chain_id``
    Integer di payload envelope (``"chain_id": 84532``), di kolom
    ``decisions.chain_id``, dan di EIP-155 saat Executor menandatangani
    transaksi.

Mandate menyimpan **string** saja. Pemetaan ke integer hidup di sini, satu
tempat, supaya ``84532`` tidak tersebar sebagai angka ajaib di beberapa modul.

Hanya ada satu entri, dan itu bukan kekurangan: PRD v2 bagian 2 menempatkan
multi-chain sebagai non-tujuan v1, dan seluruh sistem adalah testnet-only.
Menambah entri kedua kelak berarti meninjau ulang Watcher, Executor, dan
verifikasi ``decimals()`` sekaligus.

Modul ini murni: tidak menyentuh database, jaringan, atau jam.
"""

from __future__ import annotations

from typing import Final

#: Nama chain -> ``chain_id`` EIP-155. Daftar tertutup.
CHAIN_IDS: Final[dict[str, int]] = {
    "base-sepolia": 84532,
}

#: Satu-satunya chain yang didukung v1.
BASE_SEPOLIA: Final = "base-sepolia"


class ChainError(ValueError):
    """Nama chain tidak dikenal. Error input, bukan keputusan."""


def is_supported_chain(name: object) -> bool:
    """True bila ``name`` adalah nama chain yang didukung."""
    return isinstance(name, str) and name in CHAIN_IDS


def chain_id_for(name: str) -> int:
    """``chain_id`` EIP-155 untuk ``name``.

    Raises:
        ChainError: bila chain-nya tidak didukung.
    """
    if not is_supported_chain(name):
        raise ChainError(
            f"chain {name!r} tidak didukung; yang didukung: {', '.join(sorted(CHAIN_IDS))}"
        )
    return CHAIN_IDS[name]


def supported_chains() -> tuple[str, ...]:
    """Nama chain yang didukung, terurut. Dipakai pesan error dan tes."""
    return tuple(sorted(CHAIN_IDS))
