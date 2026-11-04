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
from typing import Final, NewType

#: Jumlah uang dalam **base units**: bilangan bulat, 1 USDC = 1_000_000.
#:
#: ``NewType``, bukan alias. Alias (``BaseUnits = int``) tidak memberi apa pun
#: ke pemeriksa tipe: setiap ``int`` akan tetap diterima. ``NewType`` membuat
#: mypy menolak ``int`` telanjang di posisi uang, sehingga satu-satunya jalan
#: masuk adalah lewat :func:`base_units`, :func:`parse_amount`, atau nilai yang
#: sudah bertipe itu.
#:
#: Yang dicegah bukan kesalahan aritmetika — ``int`` tetap ``int`` saat runtime
#: dan tidak ada ongkos tambahan. Yang dicegah adalah **kekeliruan satuan**:
#: menyerahkan desimal USDC (``25``) ke tempat yang mengharapkan base units
#: (``25_000_000``), atau sebaliknya. Kekeliruan itu bukan hipotetis di repo
#: ini: M4 pernah membaca kolom base units dengan pembaca desimal, dan setiap
#: batas diam-diam dikalikan sejuta tanpa satu pun pengecualian.
BaseUnits = NewType("BaseUnits", int)

#: Jumlah uang sebagai **desimal USDC ternormalisasi**, selalu enam desimal,
#: mis. ``"25.000000"``. Bentuk untuk mata manusia dan untuk mandate.
#:
#: Dibedakan dari ``str`` biasa dengan alasan yang sama: ``"25"`` dan
#: ``"25000000"`` dua-duanya string, dan hanya satu yang berarti 25 USDC.
UsdcAmount = NewType("UsdcAmount", str)

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


def base_units(value: int) -> BaseUnits:
    """Satu-satunya pintu dari ``int`` telanjang ke :data:`BaseUnits`.

    Dipakai di batas sistem tempat angka datang tanpa tipe: kolom
    ``NUMERIC(78,0)`` dari database, dan string digit dari payload envelope.
    Nilainya divalidasi di sini, jadi melewati pintu ini bukan sekadar
    menenangkan pemeriksa tipe.

    Fungsi publik lain di modul ini **tidak** menerima ``int`` telanjang untuk
    nilai uang. Itu disengaja: setiap tempat yang memanggil fungsi ini adalah
    tempat yang perlu dibaca manusia untuk memastikan satuannya benar, dan
    jumlahnya sedikit serta bisa dicari.

    Raises:
        MoneyError: bila ``value`` bukan ``int``, atau negatif.
    """
    _require_base_units(value)
    return BaseUnits(value)


def parse_amount(text: str) -> BaseUnits:
    """Ubah jumlah USDC desimal menjadi base units.

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
    total = int(whole_text) * BASE_UNITS_PER_USDC + int(fraction_digits)

    if total <= 0:
        raise MoneyError(f"jumlah {text!r} harus lebih besar dari nol")
    return BaseUnits(total)


def format_amount(amount: BaseUnits) -> UsdcAmount:
    """Tulis base units sebagai desimal ternormalisasi, selalu 6 desimal.

    ``25_000_000`` menjadi ``"25.000000"``. Bentuk ini yang dipakai di
    tampilan operator dan di mandate ternormalisasi, supaya dua mandate yang
    maknanya sama menghasilkan byte yang sama.

    Raises:
        MoneyError: bila ``amount`` bukan ``int`` atau negatif.
    """
    _require_base_units(amount)
    whole, fraction = divmod(amount, BASE_UNITS_PER_USDC)
    return UsdcAmount(f"{whole}.{fraction:0{USDC_DECIMALS}d}")


def format_base_units(amount: BaseUnits) -> str:
    """Tulis base units sebagai bilangan bulat desimal, mis. ``"25000000"``.

    Dipakai di tempat yang butuh bilangan bulat sebagai string: JSON kanonik
    mandate, dan payload envelope di M2.

    Raises:
        MoneyError: bila ``amount`` bukan ``int`` atau negatif.
    """
    _require_base_units(amount)
    return str(amount)


def _require_base_units(amount: int) -> None:
    """Validasi nilai base units. Menerima ``int`` karena ia penjaga pintunya.

    Inilah satu-satunya fungsi di modul ini yang boleh menerima ``int``
    telanjang: ia dipanggil dari :func:`base_units`, yang memang pintu masuk,
    dan dari fungsi penulis yang nilainya sudah bertipe :data:`BaseUnits`.
    Pemeriksaan runtime tetap ada karena ``NewType`` tidak memeriksa apa pun
    saat program berjalan.
    """
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise MoneyError(f"base units harus int, bukan {type(amount).__name__}")
    if amount < 0:
        raise MoneyError(f"base units tidak boleh negatif: {amount}")
