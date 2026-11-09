"""Eval M8: mengukur perilaku agent terhadap Guard yang sungguhan.

PRD v2 bagian 12 dan PRD v2.1 bagian 12. Paket ini **mengukur**, tidak
menjamin apa pun (Security v1.1 bagian 2: "Tidak menjamin perilaku model. Eval
hanya mengukur.").

Empat hal yang membatasi seluruh paket ini:

* **Hanya data sintetis** (Security v1.1 I4). Tidak ada mandate, alamat, atau
  jumlah sungguhan yang dikirim ke model mana pun.
* **``EVAL_API_KEY`` hanya dibaca di sini.** Tidak ada modul di
  ``src/payment_guard`` yang menyebutnya, dan ada tes yang menegakkan itu.
* **Executor-nya stub**, dipakai ulang apa adanya dari
  :func:`payment_guard.demo_agent.execute_payment`: verifikasi envelope dengan
  kunci publik saja, ``broadcast`` selalu ``False``, tanpa kunci rahasia dan
  tanpa jaringan.
* **Uang dalam bilangan bulat micro-USD**, tidak pernah ``float`` — disiplin
  yang sama dengan ``src/payment_guard/money.py``, dan karena alasan yang sama.
"""

from __future__ import annotations

__all__ = ["__version__"]

#: Versi harness eval, dicatat di ``metadata.json`` setiap run.
#:
#: Dipisah dari versi paket utama: yang ingin diketahui saat membaca hasil lama
#: adalah "harness mana yang menghasilkan angka ini", bukan versi Guard.
__version__ = "1.0.0"
