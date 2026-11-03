r"""Menampilkan teks dari agent sebagai data, bukan sebagai instruksi.

Security v1.1 T-8 dan ST-35..ST-37, PRD v2.1 bagian 3 nomor 10.

Field ``reason`` dikarang agent. Ia mendarat di ``decisions.reason_text`` dan
satu-satunya tempat ia pernah dilihat manusia adalah Approvals CLI. Di situlah
ia berbahaya: operator yang sedang memutuskan uang adalah sasaran yang tepat
untuk kalimat seperti "abaikan aturan, ini sudah disetujui pemilik".

Dua pertahanan di sini, dan keduanya tampilan, bukan semantik:

**Daftar-izin karakter.** Yang lolos hanya kategori Unicode L (huruf), N
(angka), P (tanda baca), S (simbol), M (tanda gabung), ditambah spasi ASCII.
Sisanya ditulis sebagai escape ``\uXXXX``. Daftar-izin, bukan daftar-tolak:
daftar-tolak selalu ketinggalan satu karakter baru.

**Bingkai.** Hasilnya selalu muncul di dalam blok berlabel, jadi tidak ada
posisi di layar tempat teks agent bisa menyamar sebagai kalimat program.

Yang **tidak** dilakukan modul ini: ia tidak menilai, tidak menyaring kata, dan
tidak mengubah makna. "abaikan aturan" tetap terbaca utuh sebagai data. Menyensor
kata justru akan menyembunyikan bukti dari operator.

Modul ini murni: tanpa database, jaringan, jam, atau berkas.
"""

from __future__ import annotations

import unicodedata
from typing import Final

__all__ = [
    "BLOCK_LABEL",
    "EMPTY_PLACEHOLDER",
    "escape_untrusted",
    "is_allowed_character",
    "render_untrusted",
]

#: Kategori Unicode yang boleh tampil apa adanya.
#:
#: Huruf, angka, tanda baca, simbol, dan tanda gabung. Yang **tidak** ada di
#: sini: C (kontrol, format, surrogate, private use, unassigned) dan Z (pemisah
#: selain spasi ASCII). Dua kelompok itulah yang dipakai untuk menggerakkan
#: kursor, membalik arah teks, dan menyelipkan karakter tak terlihat.
ALLOWED_CATEGORIES: Final[frozenset[str]] = frozenset({"L", "N", "P", "S", "M"})

#: Satu-satunya pemisah yang lolos. Spasi ASCII, bukan "spasi" lain.
#:
#: U+00A0 (no-break space) dan kerabatnya sengaja ikut di-escape: mereka
#: terlihat persis seperti spasi tapi tidak berperilaku sama, dan perbedaan
#: yang tidak terlihat adalah bahan baku penipuan tampilan.
ALLOWED_SPACE: Final = " "

#: Label blok. Dibaca operator sebelum isinya.
BLOCK_LABEL: Final = "[data dari agent, bukan instruksi]"

#: Penutup blok, supaya awal dan akhir data tidak ambigu.
BLOCK_END: Final = "[akhir data]"

#: Ditampilkan bila teksnya kosong, supaya blok kosong tidak terbaca sebagai
#: kegagalan membaca.
EMPTY_PLACEHOLDER: Final = "(kosong)"

#: Di atas nilai ini satu karakter tidak muat di ``\uXXXX``; dipakai ``\UXXXXXXXX``.
_BMP_MAX: Final = 0xFFFF


def is_allowed_character(char: str) -> bool:
    """True bila ``char`` boleh tampil apa adanya."""
    if char == ALLOWED_SPACE:
        return True
    return unicodedata.category(char)[0] in ALLOWED_CATEGORIES


def escape_untrusted(text: str) -> str:
    """Ganti setiap karakter di luar daftar-izin dengan escape-nya.

    Hasilnya selalu satu baris: newline dan carriage return ikut di-escape,
    jadi teks agent tidak bisa menambah baris ke layar operator.

    Args:
        text: teks mentah dari agent.

    Returns:
        Teks yang aman ditampilkan, tanpa karakter kontrol tersisa.

    Raises:
        TypeError: bila ``text`` bukan ``str``.
    """
    if not isinstance(text, str):
        raise TypeError(f"teks tidak tepercaya harus str, bukan {type(text).__name__}")
    return "".join(char if is_allowed_character(char) else _escape(char) for char in text)


def render_untrusted(text: str) -> str:
    """Bingkai teks agent sebagai blok data berlabel.

    Inilah satu-satunya cara ``reason`` boleh sampai ke layar operator.

    Args:
        text: teks mentah dari agent.

    Returns:
        Blok tiga baris: label, isi berawalan ``|``, lalu penutup.

    Raises:
        TypeError: bila ``text`` bukan ``str``.
    """
    body = escape_untrusted(text) or EMPTY_PLACEHOLDER
    return f"{BLOCK_LABEL}\n| {body}\n{BLOCK_END}"


def _escape(char: str) -> str:
    r"""Tulis satu karakter sebagai escape heksadesimal.

    ``\uXXXX`` untuk BMP dan ``\UXXXXXXXX`` di atasnya. Bentuknya sengaja
    sama dengan escape Python supaya operator yang penasaran bisa menyalinnya
    ke interpreter dan melihat sendiri karakter apa itu.
    """
    code = ord(char)
    if code > _BMP_MAX:
        return f"\\U{code:08X}"
    return f"\\u{code:04X}"
