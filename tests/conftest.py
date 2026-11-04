"""Konfigurasi pytest tingkat ``tests/``.

Satu-satunya tugasnya: membuat direktori ``tests/`` bisa diimpor, supaya
pembantu yang dipakai **baik** tes unit **maupun** tes integrasi hidup di satu
berkas dan bukan dua salinan yang bisa menyimpang.

pytest menambahkan direktori dasar tiap berkas tes ke ``sys.path`` (mode impor
prepend), dan karena ``tests/unit`` serta ``tests/integration`` tidak punya
``__init__.py``, direktori dasarnya adalah masing-masing subdirektori itu —
bukan ``tests/``. Penambahan di bawah ini dilakukan secara terang-terangan,
bukan diandalkan sebagai efek samping dari keberadaan berkas ini.

Yang dibagikan sejauh ini: :mod:`fake_chain`, klien chain tiruan. Tidak ada
tes di repo ini yang menyentuh jaringan.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
