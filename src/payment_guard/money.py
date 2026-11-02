"""Uang: parser dan formatter USDC. Bilangan bulat base units, tanpa float.

USDC punya 6 desimal, jadi 1 USDC = 1_000_000 base units. Semua jumlah di
dalam sistem ini adalah ``int`` base units, dari parser sampai ke database
(``NUMERIC(78,0)``). Tidak ada ``float`` di jalur mana pun: ``0.1 + 0.2`` tidak
sama dengan ``0.3`` dalam biner, dan uang tidak boleh bergantung pada itu.

Modul ini murni: tidak menyentuh database, jaringan, atau jam.

**Parser tidak pernah membulatkan.** Masukan dengan lebih dari 6 desimal
ditolak, bukan dibulatkan: pembulatan diam-diam akan membuat jumlah yang
disetujui berbeda dari jumlah yang diminta. Semua penolakan di sini adalah
**error input** (PRD v2.1 bagian 6.4 langkah 0), bukan keputusan; ia tidak
pernah tercatat di ``decisions``.
"""

from __future__ import annotations

import re
from typing import Final

#: Jumlah desimal USDC.
USDC_DECIMALS: Final = 6

#: 1 USDC dalam base units.
BASE_UNITS_PER_USDC: Final = 10**USDC_DECIMALS

#: Bentuk desimal yang diterima, dan hanya itu.
#:
#: Setiap bagian pola ini menolak satu kelas masukan yang berbahaya:
#:   ``\A``/``\Z``        tanpa spasi atau karakter lain di tepi
#:   ``0|[1-9][0-9]*``    tanpa leading zero ("007"), tanpa tanda, tanpa kosong
#:   ``[0-9]{1,6}``       maksimum 6 desimal, minimum satu ("1." ditolak)
#:   tanpa ``e``/``E``    tanpa notasi eksponen
#:
#: "NaN" dan "Infinity" ditolak karena bukan digit sama sekali.
#:
#: ``\Z`` dipakai, BUKAN ``$``: di Python ``$`` juga cocok tepat sebelum
#: newline di akhir string, jadi jumlah yang diakhiri newline akan lolos.
#: ``\Z`` hanya cocok di ujung string yang sebenarnya.
AMOUNT_PATTERN: Final = re.compile(r"\A(0|[1-9][0-9]*)(\.[0-9]{1,6})?\Z")


class MoneyError(ValueError):
    """Masukan jumlah tidak sah.

    Error input, bukan keputusan: pemanggil mengembalikannya sebagai error
    tool dan tidak menulis apa pun ke ``decisions``.
    """


def parse_amount(text: str) -> int:
    """Ubah jumlah USDC desimal menjadi ``int`` base units.

    Hanya menerima ``str``. Angka yang sudah berupa ``int`` atau ``float``
    ditolak dengan sengaja: ``float`` tidak boleh masuk jalur uang sama sekali,
    dan ``int`` tanpa satuan ambigu (apakah 25 berarti 25 USDC atau 25 base
    units?).

    Args:
        text: jumlah desimal, mis. ``"25.5"``, ``"0.000001"``, ``"50.000000"``.

    Returns:
        Jumlah dalam base units, selalu > 0.

    Raises:
        MoneyError: bila bentuknya tidak sesuai :data:`AMOUNT_PATTERN`, atau
            bila hasilnya nol.
    """
    if isinstance(text, bool) or not isinstance(text, str):
        raise MoneyError(f"jumlah harus berupa string desimal, bukan {type(text).__name__}")

    match = AMOUNT_PATTERN.match(text)
    if match is None:
        raise MoneyError(
            f"jumlah {text!r} tidak sah: harus desimal tanpa tanda, tanpa spasi, "
            f"tanpa eksponen, tanpa leading zero, maksimum {USDC_DECIMALS} desimal"
        )

    whole_text, fraction_text = match.group(1), match.group(2)
    # Pad ke kanan supaya "25.5" menjadi 500000, bukan 5.
    fraction_digits = (fraction_text[1:] if fraction_text else "").ljust(USDC_DECIMALS, "0")
    base_units = int(whole_text) * BASE_UNITS_PER_USDC + int(fraction_digits)

    if base_units <= 0:
        raise MoneyError(f"jumlah {text!r} harus lebih besar dari nol")
    return base_units


def format_amount(base_units: int) -> str:
    """Tulis base units sebagai desimal ternormalisasi, selalu 6 desimal.

    ``25_000_000`` menjadi ``"25.000000"``. Bentuk ini yang dipakai di
    tampilan operator dan di mandate ternormalisasi, supaya dua mandate yang
    maknanya sama menghasilkan byte yang sama.

    Raises:
        MoneyError: bila ``base_units`` bukan ``int`` atau negatif.
    """
    _require_base_units(base_units)
    whole, fraction = divmod(base_units, BASE_UNITS_PER_USDC)
    return f"{whole}.{fraction:0{USDC_DECIMALS}d}"


def format_base_units(base_units: int) -> str:
    """Tulis base units sebagai bilangan bulat desimal, mis. ``"25000000"``.

    Dipakai di tempat yang butuh bilangan bulat sebagai string: JSON kanonik
    mandate, dan payload envelope di M2.

    Raises:
        MoneyError: bila ``base_units`` bukan ``int`` atau negatif.
    """
    _require_base_units(base_units)
    return str(base_units)


def _require_base_units(base_units: int) -> None:
    if isinstance(base_units, bool) or not isinstance(base_units, int):
        raise MoneyError(f"base units harus int, bukan {type(base_units).__name__}")
    if base_units < 0:
        raise MoneyError(f"base units tidak boleh negatif: {base_units}")
