# Catatan keputusan

Keputusan yang menyimpang dari brief, atau yang diambil tanpa brief, beserta
alasannya. Satu entri per keputusan, yang terbaru di atas.

Berkas ini bukan ringkasan riwayat git. Yang masuk ke sini hanya keputusan yang
**tidak terbaca dari kodenya**: kode memberi tahu apa yang berlaku, bukan apa
yang ditolak dan mengapa.

---

## M6 — Watcher tidak mencocokkan apa pun

Brief M6 meminta Watcher "mencocokkan setiap transfer ke execution yang punya
`tx_hash` sama". Itu **tidak bisa dilakukan Watcher**, dan larangannya ada di
berkas yang tidak boleh diubah: `0002_roles.sql` tidak memberi `watcher_rw`
hak apa pun pada `executions` maupun `decisions` — bukan hanya UPDATE, tapi
SELECT sekalipun. Matriks di Arsitektur v1.1 bagian 3.3 menuliskannya dengan
tanda hubung di kedua kolom.

Itu bukan kelalaian matriks. PRD v2 bagian 11 menempatkan pencocokan di
Reconciler, yang punya SELECT pada ketiganya, dan pemisahannya ada supaya
proses yang **melaporkan** ketidakcocokan bukan proses yang juga bisa membuat
ketidakcocokan itu hilang.

Yang dikerjakan: Watcher mencatat **setiap** log Transfer dari wallet Executor
sebagai satu baris `settlements`, tanpa menyaring berdasarkan ada tidaknya
padanan — jadi tidak ada transfer yang diabaikan diam-diam, yang memang
maksud brief. Reconciler yang menamainya `matched`, `mismatch`, atau
`unmatched_onchain`.

Tidak ada yang di-STOP di sini karena tidak ada tabrakan dengan dokumen:
yang bertabrakan adalah brief dengan dokumen, dan dokumen yang menang.

## M6 — penyaringan per pengirim, bukan per penerima

Brief menulis "log Transfer USDC **ke alamat penerima**". Yang dipakai adalah
`from = alamat wallet Executor`, sesuai PRD v2 bagian 10 ("Hanya memantau event
`Transfer` USDC dengan `topic1 = from = alamat wallet Executor`") dan PRD v2.1
baris tabel Watcher ("Filter `from` = wallet Executor").

Perbedaannya bukan selera. Menyaring per penerima berarti transfer dari wallet
kami ke tujuan yang **tidak** kami kenal tidak pernah terlihat — padahal itu
persis temuan `unmatched_onchain`, dan satu-satunya alarm di seluruh sistem
yang berarti "uang bergerak lewat jalur di luar Payment Guard". Penyaringan per
pengirim membuatnya terlihat; penyaringan per penerima menyembunyikannya.

Penerima tetap dibandingkan — di Reconciler, terhadap `decisions.recipient`.

## M6 — tiga variabel konfigurasi di luar tabel Arsitektur 2.1

Tabel variabel di Arsitektur v1.1 bagian 2.1 tidak memuat ketiganya. Dokumen
tidak diubah; variabelnya ditambahkan dan dicatat di sini, pola yang sama
dengan "Catatan turunan" di `payment_guard.privileges`.

**`EXECUTOR_WALLET_ADDRESS`.** PRD v2 bagian 10 mewajibkan penyaringan per
alamat wallet Executor, tapi tidak ada jalan bagi Watcher untuk mengetahui
alamat itu. Ia tidak boleh membaca berkas kunci wallet (Arsitektur bagian 2
menaruh "Kedua key" di kolom yang tidak boleh dipakai Watcher) dan tidak punya
SELECT pada `executions`. Alamat adalah informasi publik; yang rahasia adalah
kuncinya. Tanpa default dengan sengaja: default apa pun di sini berarti
memantau wallet yang salah dalam diam.

**`WATCHER_INTERVAL_SECONDS`.** Dokumen menyebut Watcher "proses panjang" tanpa
menyebut jedanya, dan `guard watch --loop` harus punya satu.

**`RECONCILE_DEADLINE_SECONDS`.** PRD v2 bagian 11 menulis "setelah batas
waktu" tanpa angka. Batas itu harus punya nilai sebelum
`executed_not_settled` bisa diputuskan, dan tidak ada angka yang benar untuk
semua orang: pendek berarti alarm palsu setiap kali chain melambat, panjang
berarti pembayaran yang hilang baru terlihat besok.

## M6 — pemindaian berhenti di kepala aman

Watcher hanya memindai blok sampai `latest - CONFIRMATIONS`, bukan sampai
kepala rantai. Brief menulis "hanya blok yang sudah melewati kedalaman
konfirmasi yang diproses", dan ini bacaan harfiahnya.

PRD v2 bagian 10 bisa dibaca lain — ia mendefinisikan `pending` sebagai keadaan
log yang baru terlihat dan `pending -> final` sebagai pemeriksaan kedalaman,
yang mengandaikan pemindaian sampai kepala. Yang dipilih adalah yang lebih
konservatif: yang tidak dipindai tidak perlu dibatalkan.

Konsekuensinya perlu diketahui dan ada di docstring modulnya: setiap baris yang
baru ditulis sudah memenuhi syarat `final` pada pemeriksaan berikutnya, jadi
`pending` bertahan tepat satu siklus. Keempat transisi reorg tetap terjangkau
lewat jendela itu dan lewat `reorged -> pending`, dan semuanya punya tes.

Yang hilang: pembayaran terlihat beberapa detik lebih lambat. Di Base dengan
blok 2 detik dan `CONFIRMATIONS=12`, sekitar 24 detik.

## M6 — Reconciler lebih awal dari peta jalan, tanpa lock

Peta jalan di PRD v2 bagian 15 menaruh Reconciler di **M7**, bukan M6. Brief
memintanya di M6. Peta jalan adalah rencana, bukan aturan perilaku, dan tidak
ada satu pun aturan Reconciler di PRD bagian 11 yang dilanggar dengan
mengerjakannya lebih awal — jadi dikerjakan, dan dicatat di sini. PRD tidak
diubah.

Reconciler **tidak mengambil lock**. Tabel lock di Arsitektur v1.1 bagian 4
adalah daftar tertutup dan tidak punya namespace untuknya;
`payment_guard.locks` pun menolak namespace di luar daftar itu. Dua Reconciler
yang jalan bersamaan aman karena `UNIQUE (subject_type, subject_id)` membuat
kesimpulan yang sama menimpa dirinya sendiri. Yang bisa terjadi hanyalah satu
baris audit ganda, dan audit yang berlebih lebih baik daripada lock yang tidak
ada di dokumen.

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
