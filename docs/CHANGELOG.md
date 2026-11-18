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

### `demo_agent.py` di luar cakupan `mypy --strict`

Dua bug ditemukan di berkas itu dalam dua hari (`fd8b44a`, `75935c0`), dan
keduanya berupa argumen yang tidak diteruskan — kelas kesalahan yang
`--strict` tangkap secara statis. Daftar modul di `.pre-commit-config.yaml`
tidak memuatnya. Sengaja belum ditambahkan: itu keputusan tersendiri, dan
commit `75935c0` menyatakannya.

### Dua daftar kategori dokumen yang harus disunting bersamaan

`tests/unit/test_repo_layout.py` menyebut kelima kategori secara eksplisit di
dua tempat, `test_no_other_markdown_sneaks_into_docs` dan
`test_every_docs_category_is_disjoint`. Setiap kategori baru menuntut
penyuntingan kedua daftar; itu sudah terjadi dua kali berturut-turut,
`DESCRIPTIVE` lalu `REFERENCE` (`d69ef97`). Satu helper yang mengembalikan
semua kategori akan menutupnya.

### `CHECK event` di database — DITUTUP dengan keputusan

**Ditutup dengan keputusan, bukan dengan migrasi.** Disetujui CoS 19 Nov 2026
dan dicatat di `docs/decisions.md`, entri **2026-11-19** (commit `1a5d63a`).

Nama event tetap dijaga di kode, `src/payment_guard/events.py`. Tidak ada
migrasi `0004` dan tidak ada `CHECK` daftar tertutup di database; kolom `event`
tetap dibatasi panjangnya saja (`char_length(event) BETWEEN 1 AND 64`).
Alasannya, berikut konsekuensinya bagi pengaman di sisi database, ada di entri
itu dan tidak diulang di sini.

Komentar "menunggu M9" di `migrations/0001_init.sql:218` **dibiarkan apa
adanya**: checksum berkas migrasi dihitung atas seluruh isi berkas, jadi
menyunting komentarnya pun akan membuat `guard db migrate` menolak berkas itu
di setiap database yang sudah dimigrasi.

### Diagram Mermaid belum pernah divalidasi lewat render

`docs/arsitektur.md` masuk tanpa render (`70fa9d3`). `mmdc` tidak terpasang
dan image resmi `ghcr.io/mermaid-js/mermaid-cli` menolak dengan "denied", jadi
yang dijalankan hanya pemeriksaan struktural. Pesan commit itu menyatakannya,
dan menyebut tampilan GitHub sebagai gantinya.

---

## Belum ditag — M9 Polish: dokumen, dan dua perbaikan agent demo

Sepuluh commit di atas `a4574e5`, 17–18 Nov 2026. Belum ditag.

**Dokumen baru.** `docs/arsitektur.md` (`70fa9d3`): dua diagram Mermaid, alur
pembayaran dan tabel database. `docs/cli.md` (`d69ef97`): referensi 17 perintah
dan 25 flag, diambil dari keluaran `--help`. `docs/demo-mcp-devnet.md`
(`5689890`): catatan demo jalur MCP di devnet. Ketiganya didaftarkan di
`tests/unit/test_repo_layout.py`, masing-masing di kategori `DESCRIPTIVE`,
`REFERENCE` (kategori baru di `d69ef97`), dan `HISTORICAL`.

**README.** `f35e19a` menambahkan bagian "Status Verifikasi". `c241ce7`
memperbaiki dua kalimat yang sudah tidak benar: status `M5` → `M6`, dan butir
"Belum ada Watcher dan Reconciler" yang bertentangan dengan tabel di bagian
baru itu.

**`docs/cli.md` diperbarui** (`fc653ec`): baris status devnet untuk
`guard serve` berubah dari "belum" menjadi sudah dijalankan. Pesan commit itu
di-`amend` sebelum di-push karena satu kuantor terlalu luas — "two
CallToolRequest per round" benar untuk dua putaran agent demo tapi tidak untuk
sesi ketiga yang hanya satu panggilan tool.

**Dua perbaikan di `src/payment_guard/demo_agent.py`.** `fd8b44a`: `--network`
tidak pernah diteruskan ke `verify`, sehingga envelope devnet (`chain_id`
31337) selalu ditolak dengan "bukan 84532". `75935c0`: bawaan `--chain` dan
`--public-keys-file` keduanya produksi — `--chain` kini diturunkan dari
`--network`, dan `--public-keys-file` wajib di luar jaringan produksi.
Keduanya menyentuh `tests/unit/test_demo_agent.py`.

**`docs/decisions.md` dan `docs/runbook.md`** (`b40ccd3`): hasil skenario (d)
dan (b), alasan (c) dipotong, dan koreksi tentang alarm reconciler. Runbook
mendapat bagian 11. Alasannya ada di `docs/decisions.md`; tidak diulang di
sini.

**`f607f91`** mencatat penggantian pesan tag `m6` di `docs/decisions.md`.

---

## m6-devnet — bukti devnet lokal, bukan testnet

Tag anotasi `31dc419` di commit `a4574e5`, 17 Nov 2026. Mencakup 20 commit
dari `771c211` sampai `a4574e5`, termasuk keduanya.

Isi pesan tagnya yang perlu diketahui peninjau:

- unit 2559 lulus di checkout bersih, 1 dilewati karena `contracts/out` tidak
  dibangun di klon; integrasi 574 lulus, 5 dilewati karena tes live;
- skenario (a) jalur bahagia, keputusan `c851d003`, dan (e) recovery,
  keputusan `577f1cbf`, keduanya dijalankan di `56c6dc0` dengan rekaman
  keadaan git tersimpan;
- run awal `4728d138` dan `06971b74` **tetap ada** di database devnet tapi
  **tidak dipakai sebagai bukti**, karena keadaan git saat run itu tidak
  terekam;
- empat pembayaran `final` dan `matched`, nol alarm, saldo seimbang 500 tUSDC.

**Yang BELUM ada di dalam tag ini**, menurut pesan tagnya sendiri: skenario
(d) reorg, (b) `unmatched_onchain`, dan (c) `mismatch`. Dua yang pertama
dijalankan **sesudah** tag dibuat dan dicatat di `b40ccd3`; (c) dipotong
dengan alasan yang ada di `docs/decisions.md`. Urutan waktunya diperiksa,
bukan diasumsikan: tag dibuat 1794895298, sedangkan `decisions.created_at`
keputusan (d) `5c5a04d3` adalah 1794896094 dan `settlements.first_seen_at`
transfer (b) adalah 1794897252 — keduanya sesudahnya, selisih terkecil 796
detik. Jadi pesan tag itu benar untuk
commit yang ditunjuknya dan lebih konservatif daripada keadaan sekarang.

Tidak ada bukti testnet publik. Itu item v1.1 (`m6-onchain`).

Dua puluh commit itu membangun devnet lokal: profile Docker dan Anvil terpin
(`772012f`), pemilih jaringan opt-in (`6c71797`), kontrak token uji
(`94d2fa6`), penerusan `network` ke jalur mandate dan signing (`c194405`),
`scripts/devnet_up.py` bertahap (`7233c75`, `775bd79`, `3e9dc84`, `98e75d6`,
`e744b50`, `01e88b9`, `ec302da`), dan perbaikan bug approve devnet
(`d5108fc`). `783233c` memperbaiki master yang merah sejak `772012f` karena
`.env.devnet.example` tidak didaftarkan di `test_gitignore`.

---

## m6 — pesan tag diganti, commit yang ditunjuk tidak

Tag `m6` **tidak pernah berpindah commit**: ia menunjuk `3184482` sebelum dan
sesudah. Yang diganti adalah objek tagnya, karena pesannya usang.

| | objek tag | tanggal tagger | commit |
|---|---|---|---|
| lama | `1956307` | 2026-11-16 | `3184482` |
| sekarang | `c8a41ba` | 2026-11-17 | `3184482` |

Pesan lama berbunyi:

```
M6 terverifikasi pada tes unit dan integrasi (PostgreSQL 16, chain tiruan).
Bukti on-chain testnet BELUM ADA. Devnet lokal BELUM ADA.
```

Kalimat terakhirnya sudah tidak benar begitu devnet lokal dibangun. Pesan
sekarang menyebut angka suite dari checkout bersih (unit 2086, integrasi 539),
tetap menyatakan tidak ada bukti on-chain testnet publik, menyatakan bukti
devnet lokal **tidak** dicakup tag ini, dan menyebut bug approve devnet yang
ditemukan **sesudah** tag lama dipasang lalu diperbaiki di `d5108fc`.

Alasan penggantiannya ada di `docs/decisions.md`; commit yang mencatatnya
`f607f91`.

**Catatan jejak.** Objek tag lama `1956307` masih ada di object database
sebagai objek menggantung saat entri ini ditulis, jadi pesan lamanya masih
bisa dibaca dengan `git cat-file -p 1956307`. Itu akan hilang pada `git gc`.
Catatan yang tahan lama adalah pesan commit `f607f91`, yang mengutip pesan tag
lama di dalam body-nya, dan entri di `docs/decisions.md`.

---

## M8 Fase 2 — eval

Belum ditag.

Ringkasan eval M8 Fase 2 (qwen3:1.7b) dipindah ke `docs/eval-results/`.

Perbaikan cooldown: satu lingkungan eval per run.

Eval Haiku 5.5 (40 run, lingkungan per run, commit `0f61824`) ditambahkan ke
`docs/eval-results/`. Sesi lama dengan cooldown lintas-run ditandai superseded.

---

## M7 — state machine reorg satu tempat, dan `guard reconcile --report`

Belum ditag. Tanpa migrasi, tanpa perubahan hak akses, tanpa perubahan skema.

**State machine reorg.** `SETTLEMENT_TRANSITIONS` menuliskan kelima transisi
PRD v2 §10 sebagai data, dan `require_allowed_transition` menolak sisanya.
Sebelumnya transisinya tersebar di empat fungsi dan daftar yang dilarang tidak
ada di mana pun. `_set_status` kini satu-satunya jalur yang menulis
`settlements.status`.

Tiga cacat yang ditemukan sambil jalan: parameter `from_final` yang tidak
pernah dipakai (dihapus), `MOVE_BLOCK` dan `MARK_REORGED` tanpa penjaga
`AND status = %s` (ditambahkan, sejajar `MARK_FINAL`), dan jalur `→ reorged`
yang ada di dua tempat (disatukan). Perilaku tidak berubah — seluruh tes reorg
M6 lulus tanpa disunting.

**`guard reconcile --report`.** Baca saja: menghitung ulang dari `settlements`,
`executions`, dan `decisions`, tanpa menulis satu baris pun ke
`reconciliations` maupun `audit_log`. Keluarannya satu layar (≤ 24 baris,
≤ 100 kolom), bahasa Indonesia, ASCII saja, hash dipotong, tanpa `RPC_URL`,
isi `.env`, atau alamat wallet penuh. Kode keluar 1 bila ada yang perlu
tindakan, sama dengan `guard reconcile`.

Baris "Mode baca saja" dicetak selalu dan menyebut bahwa ini hitungan segar —
bukan isi tabel `reconciliations`. Keduanya bisa berbeda, dan itu memang
tujuannya.

**Batas hak akses yang baru terlihat.** Settlement yang terlihat sebelum
baris `executions`-nya ada mulai sebagai `unmatched_onchain` dengan
`execution_id` NULL, lalu berubah menjadi `matched` ketika execution-nya
menyusul. `reconciler_rw` hanya boleh `UPDATE (state, detail, updated_at)`,
jadi kolom tautan itu **tidak bisa diisi belakangan**. Tidak diperbaiki dengan
migrasi — itu keputusan pemilik. Informasinya dibawa di `detail`, yang boleh
di-UPDATE.

**Tes.** 115 unit di `test_watcher.py` dan 61 di `test_reconciler.py`
(termasuk setiap transisi yang diizinkan, setiap yang dilarang sebagai daftar
tertutup, dan bentuk laporan). 17 integrasi baru: laporan tidak menulis apa
pun, kasus urutan terbalik, dan transisi terlarang ditolak dengan baris
database yang tetap utuh.

**DITUNDA, disetujui pemilik:** bukti on-chain untuk `unmatched_onchain` dan
`mismatch`, dan tes ketahanan reorg terhadap node sungguhan. Wallet Executor
punya USDC tapi belum punya ETH gas. **Tag `m6` tetap ditahan.** Rinciannya di
`docs/decisions.md`.

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
