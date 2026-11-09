# Changelog

Per tag, yang terbaru di atas. Isinya yang perlu diketahui orang yang memakai
atau meninjau tag itu — termasuk yang salah di dalamnya.

Alasan di balik keputusan ada di `docs/decisions.md`; berkas ini hanya mencatat
apa yang terjadi pada tag dan commit.

---

## Utang terbuka

Hal yang **sengaja** belum dikerjakan, dengan alasannya. Dihapus dari daftar
ini hanya ketika benar-benar selesai.

### Celah alamat wallet sebelum pembayaran pertama — DITUTUP dengan checklist

**Ditutup dengan prosedur, bukan dengan kode.** Disetujui CoS 6 Nov 2026.

Penjaga saat start dari M6.2 membandingkan `EXECUTOR_WALLET_ADDRESS` dengan
`from_addr` di `executions`. Sebelum pembayaran pertama tabel itu kosong, jadi
penjaganya melewatkan pemeriksaan — dengan sengaja, karena menolak start di
sana berarti Watcher tidak bisa dijalankan sebelum pembayaran pertama.

Yang menutupnya adalah checklist operator di `docs/runbook.md` bagian 10:
bandingkan alamat yang dicetak `guard execute` (dari berkas kunci) dengan yang
dicetak `guard watch` (dari `.env`), tanpa peka huruf besar/kecil, sekali
sebelum pembayaran pertama.

**Ini prosedur manusia, dan itu perlu disebut jujur.** Prosedur bisa dilewati,
dan tidak ada yang akan menghentikan operator yang melewatinya. Yang menahan
sesudah pembayaran pertama adalah penjaga saat start; yang melaporkan
belakangan adalah `executed_not_settled` di Reconciler. Keduanya tidak menutup
jendela sebelum pembayaran pertama.

Tidak ditutup dengan kode karena satu-satunya sumber lain untuk alamat itu
adalah berkas kunci Executor, dan Watcher tidak boleh membacanya (Arsitektur
bagian 2). Lihat `docs/decisions.md`.

---

## M6.7 — Watcher mengingat lebar rentang yang diterima node

Belum ditag. Perubahan kode di `watcher.py` saja; tanpa migrasi, tanpa
perubahan hak akses, tanpa perubahan `SCAN_BATCH_SIZE` default maupun `.env`.

Lebar `eth_getLogs` yang terakhir diterima node disimpan di memori proses dan
dipakai sebagai lebar awal potongan berikutnya. Batas node ditemukan **sekali
per proses**, bukan sekali per potongan.

| Satu batch 2000 blok | Permintaan | Terbuang | Waktu |
|---|---|---|---|
| Sebelum | 52 | 38 (73%) | 9,4 menit |
| Sesudah (lebar diketahui) | **10** | **0** | **8 detik** |

**Koreksi atas catatan M6.6.** Catatan pelaksana di entri M6.6 memakai laju
chain ~30 blok/menit (asumsi blok 2 detik) dan menyimpulkan `--loop` akan
mengejar sendiri. Laju sebenarnya **~340 blok/menit**: Watcher berjalan 214
blok/menit, yaitu **0,6× laju chain**, jadi ia tertinggal permanen dan `--loop`
sendiri tidak cukup. Asumsi itu keliru 11× dan kesimpulannya terbalik.

Diagnosis (read-only, endpoint publik bawaan karena `RPC_URL` kosong): node
menerima sampai **200 blok** (batas sebenarnya 202–209), menolak di atasnya
dengan `'too large'`. **Penolakan ~14,5 detik, penerimaan ~0,75 detik** — 19×
lebih lambat, jadi setiap tebakan lebar yang salah sangat mahal.

Yang tidak berubah: pengecilan separuh, `SCAN_RANGE_FLOOR`,
`scan_max_attempts`, penanganan kegagalan non-rentang, dan `_store_batch`
beserta aturan "kursor hanya maju bersama batch yang ditulis".

Batas yang diketahui: cold start menemukan 125, bukan 200, dan ingatan tidak
pernah mencoba naik lagi — 62% kapasitas, masih jauh di atas laju chain.
Alasannya di `docs/decisions.md`.

**Opsi (b), ganti endpoint RPC, tetap keputusan pemilik** dan tidak dikerjakan.

Runbook §8.3 langkah 1 diperbaiki: ia menyuruh membuka berkas kunci untuk
membaca alamat, padahal M6.3.1 sudah menyediakan `guard wallet`. Pola itu yang
ditolak M6.3.1 sebagai opsi B.

---

## M6.6 — kursor watcher dimajukan lagi (dokumen saja)

Belum ditag. **Tidak ada perubahan kode, migrasi, maupun hak akses.**
Runbook tidak diubah.

Tambahan atas M6.5. Kursor `watcher_state` chain 84532 dimajukan ke
**55124304** (blok aman 55124804 saat pengecekan, dikurangi margin 500).
Keputusan pemilik 6 Nov 2026.

Selisih kursor terhadap blok aman membesar dari **4.336** menjadi **5.626**
blok dalam satu siklus. Blok aman sendiri bergerak 9.126 blok antara kedua
pengecekan — sekitar 5 jam waktu chain pada Base dengan blok 2 detik.

Buktinya sama dengan M6.5: wallet Executor belum pernah mengirim transaksi apa
pun, diperiksa ulang di dua tab explorer. Syarat berakhirnya juga sama —
**kalau ada transaksi keluar, pengecualian ini tidak berlaku.**

**Catatan pelaksana, bukan bagian dari keputusan.** Ini pengecualian kedua
dalam satu hari dan selisihnya membesar, jadi yang diperbaiki adalah gejalanya.
Base Sepolia menghasilkan ~30 blok/menit; `guard watch --loop` dengan batch
2.000 dan interval 15 detik memindai ~8.000 blok/menit, dan backlog 5.626 blok
akan selesai dalam sekitar tiga siklus. Yang membuat selisih tumbuh adalah
Watcher dijalankan satu siklus per pemanggilan, bukan Watcher yang terlalu
lambat. Perbaikan strukturalnya `--loop`, bukan pengecualian ketiga; setiap
pengecualian melewatkan blok secara permanen dan membakar satu-satunya alasan
yang membuatnya aman. Rinciannya di `docs/decisions.md`.

---

## M6.5 — pengecualian kursor watcher (dokumen saja)

Belum ditag. **Tidak ada perubahan kode, migrasi, maupun hak akses.**

Kursor `watcher_state` chain 84532 dimajukan ke **55115178** (blok aman
terakhir 55115678 dikurangi margin 500). Keputusan pemilik 6 Nov 2026, opsi B.
Kursor sebelumnya 54238123, backlog **877.055 blok** — sekitar 877 siklus
dengan `SCAN_BATCH_SIZE` 1000, terlalu lambat untuk kejadian pembayaran
pertama.

**Menyimpang dari runbook §8.4, yang melarang memajukan kursor.** Dicatat
sebagai pengecualian dengan buktinya, bukan sebagai pelonggaran aturan.
Buktinya: wallet Executor belum pernah mengirim transaksi apa pun, diperiksa di
explorer pada dua tab — **Transactions** dan **Token Transfers**.

Kedua tab itu perlu, dan alasannya ada di `docs/decisions.md`: log `Transfer`
dengan `from = wallet` bisa muncul dari transaksi yang **dikirim orang lain**
(`transferFrom` oleh pihak yang pernah diberi `approve`), sehingga "belum
pernah mengirim transaksi" tetap benar sementara transfer keluarnya ada. Tab
Token Transfers yang menutup celah itu.

Syaratnya ikut dicatat: **kalau ada transaksi keluar di masa depan,
pengecualian ini tidak berlaku lagi.** Runbook §8.4 mendapat catatan yang
menunjuk ke `decisions.md` dan menegaskan larangannya tetap berlaku sebagai
aturan. Langkah lain di §8.4 tidak diubah.

Yang dikerjakan **pemilik**, bukan pelaksana: pembacaan explorer dan perintah
yang memajukan kursor di cluster operator. Pekerjaan ini dokumen saja.

---

## M6.4 — batas awal pemindaian wallet executor (dokumen saja)

Belum ditag. **Tidak ada perubahan kode, migrasi, maupun hak akses.**

Batas bawah `--from-block` untuk wallet Executor ditetapkan ke blok
**54235999**: transfer USDC masuk pertama ada di **54236499** (faucet Circle,
20 USDC), dikurangi margin 500 sesuai runbook 8.2. Disetujui pemilik
6 Nov 2026, opsi A.

Keputusan itu **menyimpang dari runbook 8.3**, yang mengasumsikan transfer ETH
pertama sebagai transaksi tertua. Wallet ini menerima USDC lebih dulu dan
belum punya ETH, jadi kata "funding" di sana tidak menunjuk transaksi yang
benar. Alasannya ada di `docs/decisions.md`.

Runbook 8.3 diperbarui dengan satu paragraf: bila wallet belum pernah menerima
ETH tetapi sudah menerima USDC, pakai transfer **masuk pertama** (ETH atau
USDC, mana yang lebih dulu), dengan syarat wallet belum pernah mengirim
transaksi apa pun. Langkah 1–6 tidak diubah.

Aturan yang sebenarnya dicari di sana adalah "transfer **keluar** pertama yang
mungkin", bukan "transfer ETH pertama" — transaksi keluar menuntut gas, jadi
transaksi masuk apa pun yang paling tua adalah batas bawah yang sah. Syarat
"belum pernah mengirim" itu yang menahannya: wallet yang sudah pernah mengirim
bisa punya transfer keluar lebih tua dari transaksi masuk mana pun yang
terlihat.

Nomor blok di dokumen lain tidak diubah. Alamat wallet sungguhan tidak ditulis
ke repo; runbook memakai placeholder, dan nilainya hidup di `.env`.

**Migrasi 0003 sudah diterapkan pemilik di cluster operator.** Probe operator
kini `bbf717ac…dccdc` dan mencatat `[[1, init], [2, roles], [3,
watcher_from_addr]]`. Itu menutup satu utang yang disebut di M6.2 dan M6.3.

---

## M6.3.1 — `guard wallet` (read-only), dan koreksi atas checklist M6.3

Belum ditag.

**Koreksi.** Langkah (a) checklist M6.3 sebelumnya memakai `guard execute`,
dikoreksi ke `guard wallet` sebelum dipakai operator.

Itu kekeliruan saya di M6.3. `guard execute` **menyiarkan transaksi** bila
seluruh ceknya lolos, dan ia tidak punya mode baca-saja — tidak ada
`--dry-run`, tidak ada `--read-only`, dan `run_execute` memanggil
`execute_decision` tepat setelah `start_executor`. Checklist itu menyuruh
operator menjalankannya hanya untuk membaca satu baris dari laporan
startup-nya, dengan catatan "perintahnya boleh gagal sesudah baris itu".
Artinya ia bersandar pada harapan bahwa salah satu cek akan menolak. Kalau
seluruh cek lolos — yang justru keadaan normal untuk keputusan yang baru
disetujui — pembayaran sungguhan terkirim.

Tidak ada yang pernah menjalankannya: checklist itu ditulis kemarin dan belum
dipakai. Tapi ia sudah ter-commit, dan itu cukup.

**Perintah baru: `guard wallet`.** Read-only. Membaca `EXECUTOR_KEY_PATH`,
menurunkan alamatnya lewat `load_wallet` yang sudah ada, mencetak satu baris
`wallet 0x…`, lalu keluar. Tanpa koneksi database, tanpa RPC, tanpa transaksi
yang dibangun maupun disiarkan.

Ia **tidak** memakai `load_executor_config`, dan itu bukan penghematan:
pemuat itu menuntut `EXECUTOR_DB_URL` dan memvalidasi `RPC_URL`, jadi
`guard wallet` akan gagal karena database yang belum di-bootstrap — dan
perintah baca yang gagal karena hal yang tidak dipakainya akan mendorong orang
kembali memakai `guard execute`. Pemuat sempit `load_wallet_key_path`
ditambahkan untuk itu, dengan pemeriksaan yang sama (berkas ada, di luar repo).

Permukaannya sengaja sekecil mungkin: satu argumen, `--env-file`. Tidak ada
`--yes`, tidak ada `--broadcast`. Perintah yang hanya bisa melakukan satu hal
tidak bisa disuruh melakukan hal lain.

25 tes unit, termasuk dua yang membuktikan ia tidak menyentuh apa pun dengan
memasang `psycopg.connect` dan `Web3` yang **gagal bila dipanggil** —
pengganti yang hanya mencatat pemanggilan akan lulus selama tidak ada yang
memeriksa catatannya.

Dua kemunculan `guard execute` yang tersisa di runbook (bagian 4 dan 5) adalah
rujukan ke perintah itu sebagai perintah **tulis**, dan tetap benar.

---

## M6.3 — checklist operator untuk celah alamat wallet

Belum ditag.

Tidak ada perubahan perilaku. `guard watch` dan `guard execute` **sudah**
mencetak alamatnya masing-masing sejak M6 dan M5; yang kurang adalah prosedur
yang menyuruh operator membandingkannya, dan satu tes yang menjaga baris
Watcher itu tetap ada.

- `docs/runbook.md` bagian 10 baru: checklist enam langkah, dengan placeholder
  — tidak ada alamat asli yang bisa disalin begitu saja.
- Satu kelas tes baru (`TestStartupPrintsTheMonitoredWallet`, 3 tes): baris
  startup memuat alamat dari **konfigurasi**, bukan nilai turunan, dan tidak
  memuat apa pun berbentuk `0x` + 64 hex.
- `docs/decisions.md` dan bagian "Utang terbuka" di atas.

Migrasi, matriks hak, PRD, dan Arsitektur tidak disentuh.

---

## M6.2 — penjaga alamat wallet lewat grant kolom-tunggal

Belum ditag.

`0003_watcher_from_addr.sql`: satu pernyataan,
`GRANT SELECT (from_addr) ON executions TO watcher_rw`. Tidak ada GRANT lain,
tidak ada perubahan tabel. Keputusan CoS, opsi A.

**Penjaganya.** Sebelum pemindaian apa pun — dan sebelum satu pun perjalanan
RPC — Watcher membaca `DISTINCT from_addr` dari `executions` dan
membandingkannya dengan `EXECUTOR_WALLET_ADDRESS` lewat `normalize_address`,
jadi alamat ber-checksum dari explorer tetap cocok. Tidak ada eksekusi: lanjut.
Semua sama: lanjut. Ada yang berbeda: menolak start, mencatat
`alarm_watcher_wallet_mismatch` di transaksinya sendiri, dan keluar dengan kode
non-nol.

Yang dicegah adalah kegagalan yang **tidak** menggagalkan apa pun: konfigurasi
yang menunjuk wallet lain membuat Watcher jalan, menyaring log per pengirim
yang salah, dan tidak pernah melihat satu pun pembayaran — tanpa pengecualian,
tanpa baris `settlements`, hanya diam.

**Matriks hak**, perubahan pertama sejak `0002_roles.sql`:

| | sebelum | sesudah |
|---|---|---|
| `executions` × `watcher` | `—` | `S (kolom from_addr saja)` |

`has_table_privilege('watcher_rw', 'executions', 'SELECT')` tetap `false`.
Ada tes terpisah untuk itu, karena setiap tes per kolom akan tetap lulus kalau
grant-nya diganti menjadi tingkat tabel.

**Dua kekeliruan saya sendiri yang ditemukan tes:**

1. `count(*)` **berhasil** dengan hak satu kolom. Saya menulis tesnya dengan
   `pytest.raises` lebih dulu dan ia gagal. Postgres memperlakukan `count(*)`
   sebagai tidak merujuk kolom mana pun, jadi hak pada satu kolom sudah cukup.
   Hak kolom menutup isi baris, bukan jumlahnya. Tesnya sekarang menyatakan
   yang benar dan menjelaskan kenapa dampaknya kecil.
2. Generator `docs/schema.md` menelan blok komentar ke dalam kolom "hak",
   karena `parse_grants` tidak memanggil `strip_comments` sementara polanya
   memakai `re.DOTALL`. `0003` adalah migrasi pertama dengan komentar panjang
   tepat sebelum pernyataannya, jadi bug itu baru terlihat sekarang.
   Diperbaiki di generatornya, bukan dengan memendekkan komentar.

**Tes.** `TestWalletAddressGuardAtStartup` dibuka dari `skip` dan tumbuh dari
3 menjadi 11 tes yang berjalan. `TestTheWatcherStillCannotReadExecutions`
digantikan `TestTheWatcherReadsOnlyFromAddr`: pernyataannya berubah dari
"tidak ada hak" menjadi "tepat satu kolom", dengan satu tes per kolom terlarang.
`test_select_is_never_granted_per_column` di `test_privileges.py` menjadi
`test_select_columns_match_matrix`, bentuknya kini sama dengan padanannya untuk
UPDATE.

Fixture `migrated` tidak lagi memeriksa daftar nama migrasi yang ditulis
tangan; ia membandingkan dengan `discover()`. Daftar tangan harus disunting
setiap kali migrasi bertambah, dan sampai disunting ia menggagalkan seluruh
suite integrasi dengan pesan yang tidak menyebut sebabnya — yang persis terjadi
saat `0003` ditambahkan.

**Operator harus menjalankan `guard db migrate` sendiri** sebelum
`guard watch`. Tidak ada proses runtime yang menerapkan migrasi, dan
`guard serve` hanya memeriksa keberadaan tabel (RR-12), bukan versinya.
Prosedurnya di `docs/runbook.md` bagian 9. Migrasi ini **belum** diterapkan di
cluster operator.

---

### B3 — cek alamat wallet saat start (DITUTUP di M6.2, opsi A)

Yang diminta: Watcher membandingkan `EXECUTOR_WALLET_ADDRESS` dengan
`from_addr` di `executions`, dan menolak start bila ada eksekusi dengan alamat
yang berbeda. Gunanya nyata — konfigurasi yang menunjuk wallet lain tidak
menggagalkan apa pun, ia hanya membuat Watcher tidak pernah melihat pembayaran.

Kenapa belum dikerjakan: itu butuh **SELECT pada `executions`**, dan
`0002_roles.sql` tidak memberikannya ke `watcher_rw`. Bukan kelalaian —
Arsitektur bagian 3.3 menaruh tanda hubung di sana dengan sengaja, supaya
proses yang menulis `settlements` tidak juga bisa membaca keputusan yang
mengizinkannya. Menambah grant itu melonggarkan pemisahan yang paling ingin
dijaga di proyek ini, dan itu bukan keputusan pelaksana.

Dua opsi untuk CoS:

| | Cara | Harga |
|---|---|---|
| **A** | Migrasi `0003` memberi `watcher_rw` SELECT pada `executions`; cek dilakukan Watcher seperti diminta. | Watcher jadi bisa membaca `executions`. Pemisahan yang dijaga M6 melemah, dan migrasi baru menyentuh matriks hak. |
| **B** | Cek dipindahkan ke Reconciler, yang **sudah** punya SELECT pada `executions` dan `settlements`. Ia melaporkan ketidakcocokan alamat sebagai temuan, bukan menolak start Watcher. | Tidak ada grant baru, tidak ada migrasi. Tapi ketidakcocokan baru terlihat saat `guard reconcile` dijalankan, bukan saat Watcher start. |

Opsi B tidak butuh perubahan hak apa pun dan karena itu yang akan dipilih
kalau tidak ada arahan lain — tapi ia **belum** dikerjakan, karena yang
diminta adalah penolakan saat start.

**Hasil: CoS memilih opsi A**, dengan hak dipersempit ke satu kolom.
Dikerjakan di M6.2; lihat entri di atas. Anjuran saya (opsi B) tidak diikuti,
dan alasannya masuk akal — Reconciler yang melaporkan belakangan tidak
mencegah Watcher memantau wallet yang salah selama berjam-jam lebih dulu.

Tes yang ditandai `skip` sudah dibuka dan dijalankan.
`TestTheWatcherStillCannotReadExecutions` sengaja gagal setelah grant berubah,
yang memang gunanya; ia digantikan `TestTheWatcherReadsOnlyFromAddr` yang
menjaga pernyataan yang lebih sempit: tepat satu kolom, bukan nol.

---

## M6.1 — aturan `--from-block`, `eth_getLogs` berpotongan, utang dokumen

Belum ditag.

**`--from-block` wajib sekali, dan tidak pernah dua kali.** Tanpa kursor di
`watcher_state`, Watcher berhenti dan menyebut flag itu; tidak ada default.
Dengan kursor, flag itu diabaikan dan peringatannya dicetak.

M6 memakai 0 sebagai default, dengan alasan bahwa memindai dari genesis lambat
tapi tidak pernah melewatkan apa pun. Itu salah untuk Base Sepolia: dari
genesis sampai kepala rantai ada puluhan juta blok, jadi kursornya praktis
tidak pernah sampai ke pembayaran hari ini. Watcher yang terlihat jalan dan
tidak akan pernah tiba lebih buruk daripada Watcher yang menolak start. Angka
yang benar hanya diketahui operator — blok di sekitar funding wallet Executor —
jadi ia yang menyebutkannya. Prosedurnya sekarang ada: `docs/runbook.md`
bagian 8, yang menutup rujukan menggantung yang dilaporkan di M6.

**`eth_getLogs` dipecah per rentang.** Lebar awal dari `SCAN_BATCH_SIZE`,
diturunkan dari 2000 ke 1000. Bila node menolak **karena lebarnya**, rentang
dibagi dua dan dicoba lagi, sampai lantai 10 blok. Pengenalannya lewat
pencocokan kata pada pesan error RPC (`range`, `too large`, `more than`,
`limit`), karena tidak ada kode error standar untuk ini. Kegagalan lain
dilempar apa adanya: node yang menolak karena filter salah akan menolak hal
yang sama dengan rentang separuh, hanya lebih lambat dan dengan pesan akhir
yang menyalahkan hal yang bukan penyebabnya.

Aturan batch tidak berubah: satu rentang = satu `eth_getLogs` + satu transaksi
yang menyimpan hasilnya dan memajukan kursor. Satu siklus sekarang bisa berisi
beberapa batch. Konsekuensinya disengaja: kalau rentang ketiga gagal, dua
rentang pertama tetap tersimpan dan kursornya tetap maju sampai akhir rentang
kedua — tidak ada blok yang terlewat, dan tidak ada blok yang ditandai sudah
dipindai padahal log-nya belum tersimpan.

**Dokumen.** Arsitektur naik ke v1.2 (nama berkas tetap `Architecture_v1.1.md`,
penomoran bagian tidak berubah): bagian 2.1 kini memuat tiga variabel env yang
ditambahkan M6. PRD v2.1 bagian 14 (peta jalan) direvisi: M6 = Watcher +
Reconciler inti, M7 = sisanya. PRD v2 tidak disunting — ia dokumen yang
ditutup, dan kepala PRD v2.1 sudah menyatakan bagian yang direvisi
menggantikan pendahulunya.

**TERTAHAN: B3,** cek alamat wallet saat start. Lihat bagian "Utang terbuka"
di bawah.

---

## M6 — Watcher dan Reconciler, **mock-tested, belum live**

Belum ditag. Tag `m6` hanya setelah tes live dan bukti on-chain ada.

Dua modul baru: `watcher.py` menulis `settlements` dan `watcher_state` dari log
`Transfer` USDC; `reconciler.py` mencocokkan `settlements` dengan `executions`
dan `decisions` dan menulis `reconciliations`. Dua perintah baru: `guard watch`
dan `guard reconcile`. Enam kejadian baru di `events.py`, semuanya nama yang
sudah ada di PRD v2 bagian 10 dan 11 — tidak ada yang dikarang.

**Yang sudah terbukti:** 49 tes integrasi terhadap Postgres 16 di cluster tes,
dengan chain tiruan. Transfer cocok, transfer dengan jumlah atau penerima
berbeda, transfer tanpa execution, blok yang diproses dua kali, kedalaman
konfirmasi di bawah dan tepat di batas, keempat transisi reorg termasuk alarm
`final -> reorged`, kursor yang mundur, execution tanpa settlement setelah
batas waktu, dan GRANT yang menolak Watcher membaca `executions` maupun
Reconciler menulis `executions`.

**Yang belum terbukti:** apa pun terhadap Base Sepolia sungguhan. Tidak ada
satu pun `eth_getLogs` yang pernah dikirim ke node nyata dari repo ini. Bentuk
log yang diasumsikan `decode_transfer_log` diuji terhadap log yang **kami**
susun, dan known-answer `topic0` ERC-20 adalah satu-satunya hal di jalur itu
yang diverifikasi ke sumber luar. Yang paling mungkin berbeda di chain nyata:
tipe yang dikembalikan web3 untuk `topics` dan `data`, dan perilaku node publik
pada rentang `eth_getLogs` yang lebar.

**Satu kekeliruan yang ditemukan tes, dan layak dicatat.** Rekonsiliasi semula
membandingkan `settlements.to_addr` dengan `executions.to_addr`. Keduanya
terdengar seperti hal yang sama dan bukan: pada transfer ERC-20, tujuan
**transaksinya** adalah kontrak token, dan penerima pembayaran ada di dalam
calldata. `executions.to_addr` karena itu selalu berisi alamat kontrak USDC.
Perbandingan itu melaporkan **setiap** pembayaran yang benar sebagai
`mismatch` — alarm yang selalu berbunyi, yang sama dengan tidak ada alarm.
Penerima sekarang dibandingkan dengan `decisions.recipient`.

Tiga variabel konfigurasi baru yang **tidak** ada di tabel Arsitektur v1.1
bagian 2.1: `EXECUTOR_WALLET_ADDRESS`, `WATCHER_INTERVAL_SECONDS`, dan
`RECONCILE_DEADLINE_SECONDS`. Alasan masing-masing ada di `docs/decisions.md`.
PRD dan Arsitektur tidak diubah.

---

## m5.1 — satu tes merah di dalam tag

`m5.1` punya satu tes merah (`test_only_example_env_files_are_tracked`) karena
`.env.test.example` belum ter-commit saat suite dijalankan. Diperbaiki di
`m5.2`. Tag `m5.1` tidak dipindahkan.

Penyebabnya bukan tesnya: tes itu membaca **indeks git**, jadi berkas `.env*`
baru lolos selama masih untracked dan baru gagal setelah masuk indeks. Suite
dijalankan sebelum `git add`, lalu commit-nya menambahkan berkas yang membuat
tes itu gagal. Pre-commit tidak menangkapnya karena pada saat itu tesnya memang
masih hijau.

Yang mencegahnya terulang adalah aturan rilis di `docs/runbook.md` bagian
"Rilis": suite penuh dijalankan pada checkout bersih, bukan di working tree
yang punya berkas untracked.

Tag `m5.1` dibiarkan menunjuk commit yang sama. Memindahkannya akan membuat tag
berhenti menunjuk apa yang sebenarnya diuji dan dilaporkan saat itu, dan
riwayat yang menyembunyikan kesalahan lebih buruk daripada riwayat yang
memuatnya.

---

## m5.2 — atribusi diperbaiki, isi tidak berubah

Commit M6.0 ditulis ulang pesannya saja, untuk mengganti baris
`Co-Authored-By: Claude Haiku 5.5` menjadi `Claude Opus 5` — model yang
sesungguhnya mengerjakannya, menurut transkrip sesinya.

Pemetaan hash:

| commit | lama | baru | tree |
|---|---|---|---|
| M6.0 | `6c8adf6` | `2c72a1d` | `078f688` (tidak berubah) |
| M5.1 | `b675851` | `b675851` (tidak disentuh) | `252b77f` (tidak berubah) |

Tag yang diarahkan ulang:

| tag | lama | baru |
|---|---|---|
| `m5.2` | `6c8adf6` | `2c72a1d` |

`m5.1` dan `m0`–`m5` tidak terdampak: commit-nya tidak berubah. Atribusi di
`b675851` sudah benar sejak semula, jadi commit itu tidak ditulis ulang.

Tree hash kedua commit dibandingkan sebelum dan sesudah, dan identik:
`git diff 6c8adf6 2c72a1d` kosong. Cadangan ada di cabang
`backup-pre-reword`.
