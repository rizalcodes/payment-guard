"""Serialisasi JSON kanonik: satu makna, satu rangkaian byte.

Dipakai dua kali dengan aturan yang sama (Arsitektur v1.1 bagian 5.1: "fungsi
kanonik sama dengan signing"):

* ``mandate.py`` -> ``mandate_hash``
* ``signing.py`` (M2) -> payload envelope yang ditandatangani

Karena itu modul ini berdiri sendiri. Kalau kedua pemakai punya implementasi
masing-masing, keduanya akan menyimpang suatu hari, dan hash atau tanda tangan
akan gagal diverifikasi tanpa alasan yang kelihatan.

Aturan bentuk kanonik:

* kunci objek diurutkan, tanpa spasi pemisah
* UTF-8, tanpa escape non-ASCII
* **hanya** ``dict``, ``list``, ``str``, dan ``int``

Tipe yang ditolak, dan alasannya:

``float``
    Uang tidak pernah float. Selain itu ``json`` menulis ``1.0`` dan ``1``
    berbeda, dan ``0.1`` tidak punya representasi biner yang eksak, jadi dua
    nilai yang "sama" bisa menghasilkan byte berbeda.
``bool``
    Di Python ``bool`` adalah subclass ``int``, jadi ``True`` akan lolos
    pemeriksaan ``int`` dan diam-diam ditulis sebagai ``true``. Bidang yang
    perlu dua keadaan memakai string eksplisit.
``None``
    "Tidak ada" dan "ada tapi null" tidak boleh bisa dipertukarkan dalam
    nilai yang di-hash. Bidang opsional dihilangkan, bukan di-null-kan.

Modul ini murni: tidak menyentuh database, jaringan, atau jam.
"""

from __future__ import annotations

import json
from typing import Final

#: Tipe yang boleh muncul sebagai nilai.
ALLOWED_TYPES: Final = (dict, list, str, int)

#: Pemisah tanpa spasi. ``json`` default-nya menambahkan spasi setelah koma.
_SEPARATORS: Final = (",", ":")


class CanonicalError(ValueError):
    """Nilai tidak bisa diserialisasi secara kanonik."""


def canonical_bytes(value: object) -> bytes:
    """Serialisasi ``value`` menjadi byte kanonik UTF-8.

    Raises:
        CanonicalError: bila ada tipe yang tidak diizinkan di mana pun di
            dalam struktur. Pesan menyebut jalur ke nilai yang bermasalah.
    """
    validate(value)
    text = json.dumps(
        value,
        sort_keys=True,
        separators=_SEPARATORS,
        ensure_ascii=False,
        allow_nan=False,
    )
    return text.encode("utf-8")


def canonical_text(value: object) -> str:
    """Seperti :func:`canonical_bytes`, tapi mengembalikan ``str``.

    Hanya untuk pesan diagnostik dan tes. Yang di-hash selalu byte.
    """
    return canonical_bytes(value).decode("utf-8")


def validate(value: object, path: str = "$") -> None:
    """Pastikan ``value`` hanya memuat tipe yang diizinkan.

    Args:
        value: nilai yang diperiksa, rekursif.
        path: jalur untuk pesan error, mis. ``$.limits.max_per_tx``.

    Raises:
        CanonicalError: pada pelanggaran pertama yang ditemukan.
    """
    # bool diperiksa lebih dulu: ia subclass int, jadi urutannya penting.
    if isinstance(value, bool):
        raise CanonicalError(
            f"{path}: bool tidak diizinkan dalam bentuk kanonik; "
            "pakai string eksplisit untuk nilai dua keadaan"
        )
    if value is None:
        raise CanonicalError(
            f"{path}: None tidak diizinkan dalam bentuk kanonik; "
            "hilangkan bidang opsional, jangan di-null-kan"
        )
    if isinstance(value, float):
        raise CanonicalError(
            f"{path}: float tidak diizinkan dalam bentuk kanonik; "
            "jumlah uang memakai int base units"
        )
    if isinstance(value, str | int):
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalError(
                    f"{path}: kunci objek harus string, bukan {type(key).__name__}"
                )
            validate(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate(item, f"{path}[{index}]")
        return
    raise CanonicalError(
        f"{path}: tipe {type(value).__name__} tidak diizinkan; "
        f"yang diizinkan: {', '.join(t.__name__ for t in ALLOWED_TYPES)}"
    )
