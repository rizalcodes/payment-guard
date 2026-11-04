# Catatan keputusan

Keputusan yang menyimpang dari brief, atau yang diambil tanpa brief, beserta
alasannya. Satu entri per keputusan, yang terbaru di atas.

Berkas ini bukan ringkasan riwayat git. Yang masuk ke sini hanya keputusan yang
**tidak terbaca dari kodenya**: kode memberi tahu apa yang berlaku, bukan apa
yang ditolak dan mengapa.

---

## M6.0.1 — rumus nonce

Rumus nonce di brief diganti karena nonce 0 tidak pernah terpakai di wallet
baru. `UNIQUE(from_addr, nonce)` tetap jaring pengaman terakhir.

Yang berlaku sekarang:

```
next_nonce = max(pending_rpc, coalesce(max_db_nonce, -1) + 1)
```

Brief M6.0 menuliskannya `max(pending_rpc, max(nonce)) + 1`. Keduanya terlihat
hampir sama, dan keduanya benar untuk wallet yang sudah punya riwayat. Yang
membedakannya adalah transaksi pertama sebuah wallet:

| | brief | yang berlaku |
|---|---|---|
| DB kosong, node di 0 | **1** | **0** |

Nonce 0 yang terlewat bukan satu transaksi yang hilang. Nonce Ethereum harus
berurutan, jadi transaksi bernonce 1 tidak akan pernah ter-mine selama nonce 0
belum terpakai — dan setiap transaksi sesudahnya menumpuk di belakangnya.
Wallet itu berhenti bisa membayar, tanpa satu pun pengecualian yang dilempar:
yang terlihat hanyalah transaksi yang tidak pernah selesai.

Sumber kekeliruannya adalah dua satuan yang terlihat sama. `max(nonce)` di
`executions` adalah nonce **terakhir yang dipakai**; `pending_rpc` sudah berupa
nonce **berikutnya**. `+ 1` karena itu melekat pada sisi database saja, dan
`coalesce(..., -1)` adalah yang membuat "belum ada apa pun" berarti 0 setelah
`+ 1` — bukan 1. `-1` dipilih karena 0 adalah nonce yang sah dan terpakai,
sehingga 0 tidak bisa sekaligus berarti "belum ada yang terpakai".

`coalesce` ditulis di SQL, bukan di Python, supaya hanya ada satu cabang
aritmetika untuk dua keadaan. Penjaga di Python tetap ada: kalau `coalesce`
suatu hari hilang dari query, baris `None` tetap tidak boleh menjadi nonce 1,
dan ada tes yang memaksanya.

Rumus ini mengurangi kesempatan tabrakan; yang **menolak** tabrakan tetap
`UNIQUE(from_addr, nonce)` di `0001_init.sql`. Itu disengaja: lantai nonce di
aplikasi hanya mencegah tabrakan yang kami sendiri sebabkan, dan jaring
terakhir harus ada di tempat yang tidak bisa dilewati oleh proses mana pun.

Diuji: empat kasus pangkal di `tests/unit/test_executor.py` (`TestNextNonce`),
dan kasus (a), (b), (c) plus baris `failed` terhadap Postgres sungguhan di
`tests/integration/test_execution_flow.py`
(`TestNonceFormulaAgainstRealRows`).

---

## M6.0 — atribusi commit yang keliru

Commit `6c8adf6` (M6.0) dibuat dengan baris `Co-Authored-By: Claude Haiku 5.5`
sesuai brief, padahal seluruh sesi yang mengerjakannya berjalan di Claude
Opus 5. Diperbaiki di M6.0.1 dengan reword, tanpa perubahan isi; pemetaan
hash-nya ada di `docs/CHANGELOG.md`.

Commit `b675851` (M5.1) ternyata sudah benar dan **tidak** disentuh. Menulis
ulang commit yang tidak perlu ditulis ulang hanya memindahkan hash dan
membatalkan tag tanpa memperbaiki apa pun.
