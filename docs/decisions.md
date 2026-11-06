# Catatan keputusan

Keputusan yang menyimpang dari brief, atau yang diambil tanpa brief, beserta
alasannya. Satu entri per keputusan, yang terbaru di atas.

Berkas ini bukan ringkasan riwayat git. Yang masuk ke sini hanya keputusan yang
**tidak terbaca dari kodenya**: kode memberi tahu apa yang berlaku, bukan apa
yang ditolak dan mengapa.

---

## M6.6 — kursor watcher dimajukan lagi (tambahan atas M6.5)

Tambahan atas [M6.5](#m65--pengecualian-kursor-watcher-dimajukan-opsi-b).
Kursor `watcher_state` chain 84532 dimajukan ke **55124304** (blok aman
**55124804** saat pengecekan, dikurangi margin 500). Keputusan pemilik,
6 Nov 2026.

### Nilai dan selisih

| | Blok |
|---|---|
| Kursor yang ditetapkan M6.5 | 55115178 |
| Blok aman saat M6.5 | 55115678 (selisih 500) |
| Blok aman saat pengecekan M6.6 | 55124804 |
| Kursor baru | **55124304** (selisih 500) |

Selisih kursor terhadap blok aman membesar dari **4.336** menjadi **5.626**
blok dalam satu siklus — angka pemilik, dari pengamatan langsung. Keduanya
menyiratkan kursor berada di sekitar **55119178** saat pengecekan terakhir,
yaitu 4.000 blok di depan nilai M6.5: Watcher memang berjalan, hanya tidak
cukup cepat untuk mengejar. (Angka 55119178 itu **turunan**, bukan hasil
pembacaan; pelaksana tidak membaca database operator.)

Blok aman bergerak 9.126 blok antara kedua pengecekan, yang pada Base dengan
blok 2 detik berarti sekitar 5 jam waktu chain.

### Bukti, sama dengan M6.5

Wallet Executor **belum pernah mengirim transaksi apa pun** — diperiksa ulang
di explorer, dua tab, Transactions dan Token Transfers. Alasan kedua tab itu
perlu ada di entri M6.5 dan tidak diulang di sini.

Karena tidak ada transfer keluar sama sekali, rentang yang dilewati tidak
memuat satu pun log yang akan dicatat Watcher. Tidak ada pembayaran yang
hilang.

Hanya tanggal pemeriksaan yang dicatat. Hasil rincinya tidak disertakan di
brief yang meminta entri ini; yang tercatat adalah kesimpulannya, dan
pembacaannya dilakukan pemilik.

### Syarat berakhirnya, tetap sama

**Kalau ada transaksi keluar, pengecualian ini tidak berlaku.** Seluruh
alasannya bersandar pada "belum pernah mengirim apa pun".

### Risiko yang perlu dicatat: ini pengecualian kedua, dan selisihnya membesar

Dua pengecualian dalam satu hari, dan selisih yang tumbuh dari 500 ke 5.626,
berarti yang diperbaiki adalah gejalanya. Memajukan kursor **melewatkan blok
secara permanen** — Watcher tidak pernah mundur sendiri (runbook §8.4) — dan
setiap pengecualian membakar satu-satunya alasan yang membuatnya aman. Alasan
itu habis pada pembayaran pertama.

Angkanya menunjukkan masalahnya bukan kecepatan Watcher:

| | Blok per menit |
|---|---|
| Base Sepolia menghasilkan blok | ~30 |
| `guard watch --loop` (batch 2.000, interval 15 detik) | ~8.000 |

Pada laju itu backlog 5.626 blok selesai dalam sekitar **tiga siklus**, kurang
dari satu menit. Yang membuat selisih membesar adalah Watcher dijalankan
**satu siklus per pemanggilan** — satu siklus memindai satu batch, lalu proses
berhenti — sementara chain terus berjalan di antara pemanggilan.

Jadi perbaikan strukturalnya `guard watch --loop`, bukan pengecualian ketiga.
Dengan `--loop`, kursor mengejar sendiri dan tidak ada blok yang dilewati;
tanpa itu, setiap jeda antar pemanggilan menjadi utang yang hanya bisa dibayar
dengan melewatkan blok lagi.

Ini catatan pelaksana, bukan bagian dari keputusan pemilik. Keputusan di atas
tetap berlaku apa adanya.

## M6.5 — pengecualian: kursor watcher dimajukan (opsi B)

Kursor `watcher_state` untuk chain 84532 dimajukan ke **55115178** (blok aman
terakhir **55115678** dikurangi margin 500). Keputusan pemilik, 6 Nov 2026,
opsi B.

**Ini menyimpang dari runbook §8.4, yang melarang memajukan kursor.** Dicatat
sebagai pengecualian, dengan alasan dan buktinya, bukan sebagai pelonggaran
aturannya.

### Mengapa

Kursor sebelumnya di **54238123**. Blok aman terakhir 55115678, jadi
backlog-nya **877.055 blok**. Dengan `SCAN_BATCH_SIZE` 1000 per siklus, itu
sekitar 877 siklus sebelum pemindaian mencapai kejadian pembayaran pertama —
terlalu lambat untuk hal yang justru ingin dilihat.

### Bukti yang membuatnya aman

Wallet Executor (`0x08bd44a1…`, alamat lengkapnya di `.env`) **belum pernah
mengirim transaksi apa pun**. Diperiksa di explorer pada **6 Nov 2026**, dua
tab:

| Tab | Yang dicari |
|---|---|
| **Transactions** | tidak ada baris dengan wallet ini di kolom `From` |
| **Token Transfers** | tidak ada transfer token **keluar** dari wallet ini |

Kedua tab itu diperiksa, dan keduanya perlu. Tab **Transactions** saja tidak
cukup: Watcher menyaring log `Transfer` per `from`, dan log dengan
`from = wallet` bisa muncul dari transaksi yang **dikirim orang lain** —
yaitu `transferFrom` oleh pihak yang pernah diberi `approve`. Dalam kasus itu
"wallet belum pernah mengirim transaksi" tetap benar sementara transfer
keluarnya ada. Tab **Token Transfers** yang menutup celah itu.

Karena tidak ada transfer keluar sama sekali, rentang blok yang dilewati
(54238124–55115177) tidak memuat satu pun log yang akan dicatat Watcher. Tidak
ada pembayaran yang hilang.

### Syarat, dan kapan pengecualian ini habis

**Kalau ada transaksi keluar di masa depan, pengecualian ini TIDAK berlaku.**
Alasannya bersandar sepenuhnya pada "belum pernah mengirim apa pun", dan
begitu wallet mengirim satu transaksi saja, kalimat itu berhenti benar. Sejak
saat itu larangan §8.4 kembali mutlak: kursor tidak dimajukan, dan rentang
yang terlewat hanya bisa dipindai ulang dengan memundurkan kursor.

Pengecualian ini juga **tidak** menjadi preseden untuk wallet lain, chain
lain, atau kursor yang sama di waktu lain. Yang bisa diulang bukan
keputusannya, melainkan buktinya: dua tab explorer, keduanya kosong di sisi
keluar.

Hanya **tanggal** yang dicatat, bukan jamnya — itu yang tersedia saat entri ini
ditulis, dan jam tidak mengubah apa pun yang bisa diperiksa ulang. Pembacaan
explorer-nya dilakukan pemilik, bukan oleh pelaksana.

## M6.4 — batas awal pemindaian wallet executor

Batas awal pemindaian wallet executor: blok **54235999** (USDC masuk pertama
**54236499**, margin 500). Menyimpang dari runbook 8.3 yang mengasumsikan ETH.
Disetujui pemilik 6 Nov 2026.

Alasannya:

- Transfer USDC **masuk** pertama ke wallet itu ada di blok 54236499 (faucet
  Circle, 20 USDC).
- Wallet belum pernah mengirim apa pun, dan belum punya ETH gas, jadi tidak ada
  transfer **keluar** yang bisa terjadi sebelum blok itu. Transaksi keluar
  menuntut gas; tanpa ETH, tidak ada satu pun yang mungkin.
- Runbook 8.3 mengasumsikan transfer **ETH** pertama sebagai transaksi tertua,
  karena itu urutan yang biasa. Di kasus ini USDC datang lebih dulu, jadi kata
  "funding" di sana tidak menunjuk transaksi yang benar.

Yang sesungguhnya dicari batas bawah itu adalah **transfer keluar pertama yang
mungkin**, bukan "transfer ETH pertama". Keduanya biasanya sama; di sini tidak.
Runbook 8.3 diperbarui untuk menyebut aturan yang sebenarnya, beserta syaratnya
— wallet belum pernah mengirim apa pun — karena wallet yang sudah pernah
mengirim bisa punya transfer keluar lebih tua dari transaksi masuk mana pun
yang terlihat.

Alamat wallet-nya **tidak** ditulis di sini. Ia memang publik, tapi ia tidak
perlu ada di repo: nilainya hidup di `.env` (gitignored), dan `guard wallet`
mencetaknya kapan pun dibutuhkan. Repo ini reference implementation yang
dibaca orang lain, dan alamat sungguhan di dalamnya hanya akan menjadi hal
yang disalin tanpa dipikir. Yang membuat keputusan ini bisa dilacak adalah
nomor bloknya, dan itu ada di atas.

Margin 500 blok mengikuti runbook 8.2: ia melindungi dari kekeliruan beberapa
blok saat membaca explorer, dan yang hilang hanya kecepatan siklus pertama.

## M6.3.1 — perintah baca terpisah untuk alamat wallet

Perintah baca terpisah lebih aman daripada memakai perintah yang menyiarkan
transaksi untuk membaca alamat. Disetujui CoS 6 Nov 2026.

Checklist M6.3 langkah (a) menyuruh menjalankan `guard execute` untuk membaca
alamat wallet dari laporan startup-nya. `guard execute` menyiarkan transaksi
bila seluruh ceknya lolos, dan tidak punya mode baca-saja. Checklist itu
bahkan menuliskan "perintahnya boleh gagal sesudah baris itu" — yaitu
bersandar pada harapan bahwa salah satu cek akan menolak. Kalau seluruh cek
lolos, yang justru keadaan normal untuk keputusan yang baru disetujui,
pembayaran sungguhan terkirim.

Tiga pilihan yang dipertimbangkan:

| | Cara | Mengapa tidak / ya |
|---|---|---|
| A | Tambah `--dry-run` ke `guard execute` | Satu flag memisahkan baca dari menyiarkan di perintah yang sama. Salah ketik, flag yang hilang dari riwayat shell, atau `--dry-run` yang tidak ikut tersalin dari dokumen — semuanya menyiarkan. Jalur baca dan jalur bayar tidak boleh berbagi satu perintah. |
| B | Baca `.env` dan berkas kunci dengan `jq`/`python` di runbook | Tidak ada perintah baru, tapi prosedurnya menyuruh operator membuka berkas kunci dengan alat umum — dan dari situ ke `cat` yang menampilkan `private_key` hanya satu salah ketik. |
| **C** | **Perintah baru `guard wallet`, read-only** | Dipilih. Tidak ada jalan dari perintah ini ke penyiaran, bahkan dengan argumen yang salah. Ia juga tidak pernah mencetak field selain alamat. |

Yang membuat C murah: tidak ada logika baru. `load_wallet` sudah ada dan sudah
memeriksa bahwa `address` di berkas cocok dengan yang diturunkan dari
kuncinya; `guard wallet` hanya memanggilnya dan mencetak satu field.

Permukaannya dijaga sempit dengan sengaja: satu argumen, `--env-file`. Ada tes
yang menolak `--yes`, `--broadcast`, `--execute`, `--force`, dan `--send`.
Bukan karena seseorang akan menambahkannya hari ini, tapi karena perintah yang
permukaannya tumbuh akan berhenti bisa dipercaya sebagai perintah baca.

## M6.3 — celah alamat wallet sebelum pembayaran pertama

Celah alamat wallet sebelum pembayaran pertama: diterima, ditutup dengan
checklist operator (runbook). Disetujui CoS 6 Nov 2026.

Penjaga saat start (M6.2) membandingkan `EXECUTOR_WALLET_ADDRESS` dengan
`from_addr` di `executions`. Sebelum pembayaran pertama tabel itu kosong, jadi
penjaganya melewatkan pemeriksaan. Itu bukan kelalaian: menolak start ketika
belum ada eksekusi berarti Watcher tidak bisa dijalankan sebelum pembayaran
pertama, padahal justru pembayaran pertama yang ingin dilihatnya.

Yang **tidak** dilakukan: menambahkan sumber ketiga untuk alamat itu supaya
bisa dibandingkan otomatis. Satu-satunya sumber lain yang ada adalah berkas
kunci Executor, dan Watcher tidak boleh membacanya (Arsitektur bagian 2
menaruh kedua kunci di kolom yang tidak boleh dipakai Watcher). Menambah
sumber ketiga yang hanya ada supaya bisa diverifikasi berarti satu nilai lagi
yang bisa diisi salah tanpa ada yang memeriksanya.

Yang dilakukan: checklist operator di `docs/runbook.md` bagian 10, sekali
sebelum pembayaran pertama. Ia bersandar pada dua baris yang **sudah** dicetak
sejak M6 — `guard execute` mencetak alamat yang diturunkan dari berkas kunci,
`guard watch` mencetak alamat dari konfigurasi — jadi tidak ada kode baru yang
perlu ditulis untuk menutupnya.

Keterbatasannya ditulis jujur di runbook: checklist adalah prosedur manusia,
dan prosedur manusia bisa dilewati. Yang menahannya sesudah pembayaran pertama
adalah penjaga saat start; yang melaporkannya belakangan adalah
`executed_not_settled` di Reconciler.

Satu tes ditambahkan meski tidak ada kode yang berubah: baris startup Watcher
kini **load-bearing** karena checklist menyuruh membacanya, dan sampai M6.3
tidak ada tes yang menjaganya. Padanannya untuk Executor sudah dijaga sejak M5.

## M6.2 — Watcher mendapat satu kolom di `executions`

Keputusan CoS, opsi A, 5 Nov 2026. `watcher_rw` mendapat `SELECT (from_addr)`
pada `executions` lewat `0003_watcher_from_addr.sql`. Tidak lebih.

Utang ini dibuka di M6.1 dengan dua opsi (`docs/CHANGELOG.md`, "Utang
terbuka"). Yang dipilih bukan yang saya anjurkan — saya menulis bahwa opsi B
(cek dipindahkan ke Reconciler) akan dipilih kalau tidak ada arahan, karena ia
tidak butuh grant baru. Opsi A dipilih, dan alasannya masuk akal: **penolakan
saat start** adalah yang diminta, dan Reconciler yang melaporkan belakangan
tidak mencegah Watcher memantau wallet yang salah selama berjam-jam lebih dulu.

Yang membuat opsi A tetap sempit:

- **Satu kolom, bukan tabel.** `has_table_privilege('watcher_rw',
  'executions', 'SELECT')` tetap `false`, dan ada tes terpisah untuk itu —
  karena setiap tes per kolom akan tetap lulus kalau grant-nya diganti menjadi
  tingkat tabel.
- **`from_addr` tidak memberi tahu Watcher apa pun tentang keputusan mana pun.**
  Ia alamat wallet kami sendiri, satu nilai yang sama di setiap baris, yang
  operator sudah menuliskannya di `.env`. Bukan `decision_id`, bukan `tx_hash`,
  bukan `amount_base`, bukan `nonce`, bukan `raw_tx`.
- **Pemisahan peran tidak berubah.** Pencocokan settlement dengan execution
  tetap pekerjaan Reconciler (PRD v2 bagian 11).

Satu sifat Postgres yang ditemukan lewat tes yang gagal, dan dicatat jujur:
`count(*)` **berhasil** dengan hak satu kolom, karena Postgres
memperlakukannya sebagai tidak merujuk kolom mana pun. Hak kolom menutup isi
baris, bukan jumlahnya. Dampaknya kecil — jumlah eksekusi bukan rahasia, dan
Watcher sudah mengetahui lebih banyak lewat `settlements` yang ia tulis
sendiri — tapi anggapan sebaliknya terasa lebih masuk akal, jadi ia diuji
alih-alih dikomentari.

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

## M6 — Reconciler lebih awal dari peta jalan: disetujui

Reconciler digeser ke M6 oleh pelaksana; disetujui CoS 5 Nov 2026.

Peta jalan di PRD v2.1 §14 sudah direvisi: M6 = Watcher dan Reconciler inti,
M7 = sisanya. PRD v2 §14 tidak disunting — ia dokumen yang ditutup, dan kepala
PRD v2.1 sudah menyatakan bagian yang direvisi menggantikan pendahulunya. Jadi
tidak ada dua peta jalan yang bertentangan: ada satu yang berlaku dan satu yang
sudah digantikan, dan §14 v2.1 kini menyebutnya eksplisit.

## M6 — Reconciler tanpa lock

Catatan awal entri ini menyebut "PRD v2 bagian 15" sebagai tempat peta jalan.
Itu salah: peta jalan PRD v2 ada di **bagian 14** ("Struktur repo & milestone"),
dan bagian 15 adalah "Definition of Done". Dikoreksi 5 Nov 2026 bersama revisi
peta jalan di atas.

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
