# Runbook operator

Prosedur manual untuk keadaan yang tidak ditangani otomatis. Testnet saja.

Runbook kunci penanda tangan (rotasi, pencabutan) ada di
[Security_v1.1.md](Security_v1.1.md) bagian 8. Berkas ini mengurus jalur
eksekusi.

> **Aturan pertama, dan tidak ada pengecualian:** jangan pernah menghapus atau
> mengubah baris `executions` secara manual. Baris itu adalah satu-satunya
> catatan bahwa sebuah transaksi mungkin sudah ada di jaringan, dan `guard
> recover` membutuhkannya. Menghapusnya tidak membatalkan apa pun di chain; ia
> hanya membuat Anda kehilangan kemampuan mengetahui apa yang terjadi.

---

## 1. Transaksi macet

### Gejalanya

Baris `executions` berstatus `broadcast`, `guard recover` dijalankan berulang
kali, dan statusnya tidak berubah:

```bash
uv run guard recover
# <decision_id>  broadcast  masih tertunda: ...
```

Artinya: node sudah menerima transaksinya, nonce-nya masih milik kita, dan
belum ada receipt. Biasanya `maxFeePerGas` di bawah base fee yang berlaku
sekarang.

Periksa keadaannya:

```bash
docker compose exec postgres psql -U guard_admin -d payment_guard -c \
  "SELECT decision_id, status, tx_hash, nonce,
          extract(epoch from now() - broadcast_at)::bigint AS umur_detik
   FROM executions WHERE status IN ('signed','broadcast') ORDER BY nonce"
```

Lihat juga di explorer: `https://sepolia.basescan.org/tx/<tx_hash>`.

### Mengapa tidak ada tombol otomatis

**v1 tidak punya bump maupun replacement.** Tidak ada perintah yang menaikkan
ongkos transaksi yang sudah disiarkan. Itu pilihan, bukan kelalaian: mengganti
transaksi berarti menandatangani transaksi **kedua** dengan nonce yang sama,
dan dua transaksi sah dengan nonce sama untuk satu pembayaran adalah tepat
keadaan yang paling berbahaya di sistem ini. Melakukannya dengan benar butuh
aturan kapan yang lama dianggap mati, dan aturan itu belum ada.

Akibatnya, transaksi macet **tetap** `broadcast` sampai salah satu dari dua hal
terjadi: ia akhirnya masuk blok, atau nonce-nya dipakai transaksi lain.

### Pilihan A: tunggu

Base Sepolia biasanya lega. Base fee turun, transaksinya masuk, dan
`guard recover` mencatatnya `confirmed`. Ini pilihan bawaan dan hampir selalu
benar.

Tidak ada batas waktu yang membuatnya `failed`. Timeout bukan kegagalan:
menandai `failed` karena lambat akan menyembunyikan pembayaran yang
sesungguhnya berhasil.

### Pilihan B: ganti dengan transaksi lain pada nonce yang sama

Hanya bila Anda memang perlu nonce itu bebas. Dikerjakan **di luar** Payment
Guard, dengan wallet atau alat Anda sendiri, memakai kunci di
`EXECUTOR_KEY_PATH`.

1. Catat `nonce` dan `tx_hash` dari kueri di atas.
2. Kirim transaksi pengganti dari alamat yang sama, **nonce yang sama**, dengan
   `maxFeePerGas` dan `maxPriorityFeePerGas` lebih tinggi. Transaksi kosong ke
   diri sendiri sudah cukup bila yang Anda inginkan hanya membebaskan nonce.
3. Tunggu sampai pengganti itu masuk blok.
4. Jalankan:

   ```bash
   uv run guard recover
   ```

   Executor akan melihat `confirmed_nonce` sudah melewati nonce kita dan tidak
   ada receipt untuk `tx_hash` kita, lalu menandai barisnya `failed` dengan
   sebab `nonce_taken`. Itu catatan yang benar: pembayaran itu tidak terjadi.

5. Keputusan yang bersangkutan **tidak bisa dieksekusi ulang**.
   `UNIQUE(decision_id)` berlaku juga untuk `failed`, dan itu disengaja —
   mencoba lagi berarti meminta persetujuan baru, bukan memakai ulang yang
   lama. Minta agent mengajukan ulang, lalu setujui lagi lewat
   `guard approvals approve`.

### Yang jangan dilakukan

- Jangan menghapus baris `executions`.
- Jangan mengubah `status` dengan SQL. Role `executor_app` memang tidak
  diberi hak itu untuk kolom selain `status`, `broadcast_at`, dan `updated_at`,
  dan `guard recover` adalah satu-satunya yang boleh menulisnya.
- Jangan menjalankan `guard execute` untuk `decision_id` yang sama "supaya
  dicoba lagi". Cek 5 akan menolaknya dengan `already_executed`, dan itu
  jawaban yang benar.

---

## 2. Margin `maxFeePerGas`

Executor menyetel:

```
maxFeePerGas        = baseFeePerGas blok terakhir * 2 + tip
maxPriorityFeePerGas = eth_maxPriorityFeePerGas
```

Pengali dua adalah anjuran EIP-1559 yang biasa: base fee bisa naik paling
banyak 12,5 persen per blok, jadi margin dua kali menahan sekitar enam blok
kenaikan berturut-turut. Yang benar-benar dibayar tetap base fee saat transaksi
masuk ditambah tip; `maxFeePerGas` hanya batas atas.

Batas gas memakai perkiraan `eth_estimateGas` ditambah 20 persen.

Angka-angka itu konstanta di `src/payment_guard/chain.py`
(`BASE_FEE_MULTIPLIER`, `GAS_LIMIT_NUMERATOR`, `GAS_LIMIT_DENOMINATOR`), bukan
konfigurasi. Kalau transaksi Anda sering macet di jaringan yang sedang sibuk,
yang perlu ditinjau adalah angka itu, dan peninjauannya bukan perubahan
konfigurasi sepihak.

---

## 3. Node RPC tidak bisa dihubungi

Executor mencoba tiga kali per panggilan, dengan jeda satu lalu dua detik, dan
batas waktu terpisah untuk menyambung (5 detik) dan membaca (20 detik). Setelah
itu ia menyerah dengan `ChainError`. Tidak ada jalur yang menunggu tanpa batas.

Bila broadcast gagal karena ini, barisnya **tetap** `signed` dan tidak ada yang
disiarkan. Perbaiki endpoint-nya, lalu:

```bash
uv run guard recover
```

Byte yang sama disiarkan ulang, jadi tidak ada pembayaran kedua.

Pesan error hanya menyebut skema dan host, bukan URL utuh: penyedia berbayar
menaruh kunci API di path atau query.

---

## 4. Nonce menurut kami dan menurut node berbeda

Executor mengambil nonce di dalam lock `executor:<from_addr>`:

```
next_nonce = max(pending_rpc, coalesce(max_db_nonce, -1) + 1)
```

Dua sumber, jadi node yang baru di-restart tidak bisa membuat nonce terpakai
dua kali. `+ 1` hanya melekat pada sisi database, karena `max(nonce)` adalah
nonce terakhir yang **dipakai** sedangkan `pending_rpc` sudah nonce
**berikutnya**. Wallet tanpa riwayat mulai di 0. Rumus lengkapnya beserta
alasannya ada di [decisions.md](decisions.md).

Yang **tidak** ditangani: transaksi dari wallet yang sama yang dikirim di luar
Payment Guard dan belum terlihat node. Kalau Anda memakai wallet Executor untuk
hal lain, berhentilah; wallet itu sebaiknya hanya dipakai satu proses.

Untuk melihat selisihnya:

```bash
docker compose exec postgres psql -U guard_admin -d payment_guard -c \
  "SELECT from_addr, max(nonce) AS nonce_terakhir_kami, count(*)
   FROM executions GROUP BY from_addr"
```

Bandingkan dengan explorer di
`https://sepolia.basescan.org/address/<alamat>`.

---

## 5. Saldo gas habis

`guard execute` dan `guard recover` tetap **start** dengan peringatan, bukan
menolak. Itu disengaja: saldo yang kurang tidak boleh menghalangi `recover`
dijalankan, justru karena di situlah ia paling dibutuhkan.

Isi dari faucet; daftarnya ada di
[../scripts/demo_execute.md](../scripts/demo_execute.md) langkah 3.

---

## 6. Memeriksa jejak

```bash
docker compose exec postgres psql -U guard_admin -d payment_guard -c \
  "SELECT actor, event, payload FROM audit_log ORDER BY id DESC LIMIT 20"
```

Daftar kejadian beserta payload-nya ada di [events.md](events.md). `audit_log`
append-only lewat trigger dan lewat hak akses; UPDATE dan DELETE ditolak untuk
semua role, termasuk admin.

---

## 7. Rilis

**Sebelum membuat tag, jalankan suite penuh pada checkout bersih.** Clone baru
ke folder scratchpad di luar repo, atau `git stash -u` di repo asal.

```bash
# Pilihan A: clone bersih (disarankan)
git clone --no-hardlinks . /path/ke/scratchpad/payment-guard-rilis
cd /path/ke/scratchpad/payment-guard-rilis
uv sync --locked --all-groups
PAYMENT_GUARD_TEST_ADMIN_DB_URL=... uv run --no-sync pytest tests/ -q

# Pilihan B: di repo asal
git stash -u          # termasuk berkas untracked
uv run --no-sync pytest tests/ -q
git stash pop
```

**Jangan menilai suite di working tree yang punya berkas untracked.** Itu
penyebab `test_only_example_env_files_are_tracked` lolos lokal tapi merah di
commit: tes itu membaca indeks git, jadi berkas `.env*` yang belum ter-`add`
tidak terlihat olehnya. Suite dijalankan, hijau, lalu `git add` memasukkan
berkas itu dan tag yang dibuat sesudahnya memuat tes merah. Kejadiannya ada di
[CHANGELOG.md](CHANGELOG.md) pada `m5.1`.

Hal yang sama berlaku untuk berkas yang **dihapus** tapi belum di-stage, dan
untuk `.venv` yang masih memuat paket dari percobaan sebelumnya. Clone bersih
menyelesaikan ketiganya sekaligus; `git stash -u` hanya dua yang pertama.

Urutan rilis:

1. Suite penuh pada checkout bersih — hijau.
2. `pre-commit run --all-files` — hijau.
3. `git status --porcelain` — kosong.
4. Commit, lalu tag.
5. Catat di [CHANGELOG.md](CHANGELOG.md), termasuk yang diketahui masih salah.

---

## 8. Menyemai `watcher_state` dan `--from-block`

Watcher menyimpan satu baris per chain di `watcher_state`: `last_scanned`,
yaitu blok tertinggi yang **sudah** dipindai. Semua yang di bawah atau sama
dengan nilai itu dianggap sudah dilihat.

### 8.1 Dua keadaan, dua perilaku

**Kursor sudah ada untuk `chain_id` itu.** Pemindaian lanjut dari
`last_scanned + 1`. `--from-block` **DIABAIKAN**, dan Watcher mencetak
peringatan:

```
PERINGATAN: --from-block 4000 DIABAIKAN. Kursor watcher_state sudah ada, dan ia
tidak pernah dipindahkan ke depan oleh flag: itu akan melewatkan blok dalam
diam. Lihat docs/runbook.md bagian 8.
```

Flag itu tidak pernah memindahkan kursor yang sudah berjalan — ke depan maupun
ke belakang. Memindahkannya ke depan akan menandai blok yang belum dipindai
sebagai sudah dipindai, dan blok yang terlewat tidak akan pernah diminta lagi.
Itu satu-satunya kesalahan di Watcher yang tidak bisa diperbaiki belakangan
oleh Watcher sendiri.

**Kursor belum ada.** `--from-block` **WAJIB**. Tanpa nilai, Watcher berhenti:

```
belum ada kursor di watcher_state untuk chain ini, jadi Watcher tidak tahu dari
blok mana harus mulai. Jalankan sekali dengan `guard watch --from-block <nomor
blok>`; pakai blok di sekitar funding wallet Executor dikurangi margin kecil,
bukan 0. Lihat docs/runbook.md bagian 8.
```

Tidak ada kursor yang ditulis dan tidak ada settlement yang dicatat ketika ia
berhenti begini. Menjalankannya lagi dengan nilai yang benar aman.

### 8.2 Angka yang dipakai saat live

Pakai **blok di sekitar funding wallet Executor, dikurangi margin kecil** —
misalnya 500 blok. **Bukan 0.**

Base Sepolia punya puluhan juta blok. Memindai dari genesis dengan rentang
1000 blok per siklus berarti puluhan ribu siklus sebelum kursornya mencapai
pembayaran hari ini: Watcher akan terlihat jalan dan tidak akan pernah tiba.

Margin 500 blok ada untuk melindungi dari satu kesalahan: salah mengira blok
funding. Transfer USDC pertama tidak bisa terjadi sebelum wallet punya gas,
jadi blok funding adalah batas bawah yang sah — dan margin kecil di bawahnya
membuat kekeliruan beberapa blok tidak kehilangan pembayaran apa pun. Margin
yang besar hanya memperlambat siklus pertama.

### 8.3 Mencari nomor blok funding

1. Ambil alamat wallet Executor:

   ```bash
   guard wallet
   ```

   Keluarannya satu baris, `wallet 0x<alamat>`. **Jangan** membuka berkas
   kuncinya untuk ini: `guard wallet` membaca berkas itu dan mencetak
   alamatnya saja, tanpa pernah menampilkan `private_key`.
2. Buka `https://sepolia.basescan.org/address/<alamat>`.
3. Pilih tab **Transactions**. Transaksi paling bawah (paling tua) adalah
   transfer ETH dari faucet.
4. Klik transaksi itu. Pada halaman detailnya, baca baris **Block**. Angka itu
   nomor blok funding.
5. Alternatif, bila hash transaksi faucet masih ada: buka
   `https://sepolia.basescan.org/tx/<hash>` langsung dan baca baris **Block**
   di sana.
6. Kurangi 500: `--from-block <blok funding − 500>`. Bila hasilnya negatif,
   pakai 0.

**Bila wallet belum pernah menerima ETH tetapi sudah menerima USDC**, gunakan
transfer **masuk pertama** (ETH atau USDC, mana yang lebih dulu) sebagai dasar.
Syarat: wallet belum pernah mengirim transaksi apa pun. Catat keputusannya di
[decisions.md](decisions.md).

Langkah 1–6 di atas mengandaikan transfer ETH dari faucet sebagai transaksi
tertua, dan itu urutan yang biasa — wallet butuh gas sebelum bisa mengirim
apa pun. Tapi urutannya tidak dijamin: faucet USDC dan faucet ETH adalah dua
layanan terpisah, dan yang satu bisa datang lebih dulu. Yang sesungguhnya
dicari batas bawah ini adalah **transfer keluar pertama yang mungkin**, dan
transfer keluar tidak bisa terjadi sebelum wallet punya gas — jadi transaksi
masuk apa pun yang paling tua tetap batas bawah yang sah.

Syarat "belum pernah mengirim" itu yang menahannya: wallet yang sudah pernah
mengirim mungkin punya transfer keluar **sebelum** transaksi masuk yang Anda
temukan, misalnya karena pernah diisi lalu dikosongkan. Di tab
**Transactions** explorer, periksa tidak ada baris dengan wallet ini di kolom
`From`. Kalau ada, pakai blok baris tertua itu, bukan transaksi masuk.

Periksa sekali lagi bahwa alamat di explorer sama dengan
`EXECUTOR_WALLET_ADDRESS` di `.env`. Alamat yang berbeda berarti Watcher
memantau wallet lain, dan tidak ada yang akan gagal — ia hanya tidak akan
pernah melihat pembayaran apa pun.

### 8.4 Kalau nilainya salah diisi

**Terlalu besar** (kursor melewati blok yang berisi pembayaran). Kursor
**tidak pernah mundur otomatis**. Satu-satunya hal yang memundurkannya adalah
reorg yang terdeteksi pada baris settlement yang sudah ada, dan blok yang
tidak pernah dipindai tidak punya baris settlement — jadi tidak ada yang bisa
terdeteksi. Pemindaian akan terus maju dari tempatnya sekarang, dan pembayaran
di bawahnya tidak akan pernah terlihat.

Memperbaikinya berarti menghapus baris kursornya, dan itu **tindakan sadar
operator**, bukan sesuatu yang dilakukan Watcher. Watcher tidak punya DELETE
pada tabel mana pun (`0002_roles.sql`), jadi perintahnya dijalankan admin:

```bash
docker compose exec postgres psql -U guard_admin -d payment_guard \
  -v ON_ERROR_STOP=1 \
  -c "DELETE FROM watcher_state WHERE chain_id = 84532"
```

Hentikan Watcher lebih dulu. Ia memegang lock sesi `watcher:<chain_id>`, dan
menghapus kursor di bawah Watcher yang sedang jalan berarti ia akan menulis
ulang kursornya dari nilai di memorinya pada siklus berikutnya.

Setelah baris itu hilang, keadaannya sama dengan belum pernah memindai:
jalankan lagi dengan `--from-block` yang benar. Baris `settlements` yang sudah
ada **tidak** dihapus dan tidak perlu dihapus — `ON CONFLICT (chain_id,
tx_hash, log_index) DO NOTHING` membuat pemindaian ulang tidak menghasilkan
baris ganda, dan baris yang sudah `final` tidak dikembalikan ke `pending`.

**Terlalu kecil.** Tidak ada yang rusak; siklus pertama hanya lebih lama.
Biarkan berjalan.

**Pengecualian terdokumentasi:** lihat [decisions.md](decisions.md)
(6 Nov 2026). Hanya berlaku bila wallet belum pernah mengirim.

Bagian ini melarang memajukan kursor, dan larangan itu tetap berlaku sebagai
aturan. Satu pengecualian pernah diberikan, dengan buktinya, dan ia dicatat
sebagai pengecualian — bukan sebagai pelonggaran aturannya. Jangan memakainya
sebagai preseden tanpa bukti yang sama: wallet yang **belum pernah mengirim
apa pun** tidak bisa punya transfer keluar di rentang yang dilewati, jadi tidak
ada pembayaran yang bisa hilang. Begitu wallet pernah mengirim satu transaksi
saja, alasan itu habis dan larangannya kembali mutlak.

### 8.5 Memulai ulang pemindaian dari awal

Yang dimaksud "dari awal" di sini adalah memindai ulang rentang blok, bukan
menghapus riwayat. Riwayat tidak dihapus sama sekali.

1. Hentikan Watcher: `Ctrl-C` pada `guard watch --loop`, atau tunggu siklus
   tunggalnya selesai. Pastikan tidak ada proses `guard watch` yang tersisa —
   Watcher kedua akan ditolak dengan pesan "sudah memegang lock".
2. Mundurkan kursor ke blok sebelum titik yang ingin dipindai ulang:

   ```bash
   docker compose exec postgres psql -U guard_admin -d payment_guard \
     -v ON_ERROR_STOP=1 \
     -c "UPDATE watcher_state SET last_scanned = 12345 WHERE chain_id = 84532"
   ```

   Nilai yang diisi adalah blok **terakhir yang sudah dipindai**, jadi
   pemindaian lanjut dari `12346`.
3. Jalankan Watcher seperti biasa. `--from-block` tidak diperlukan dan akan
   diabaikan: kursornya ada.
4. Periksa hasilnya:

   ```bash
   docker compose exec postgres psql -U guard_admin -d payment_guard -c \
     "SELECT status, count(*) FROM settlements GROUP BY status"
   ```

**Jangan** menghapus baris `settlements` untuk "membersihkan" hasilnya. Aturan
pertama runbook ini berlaku di sini juga: yang pernah terlihat di chain tetap
tercatat pernah terlihat. Pemindaian ulang idempoten, jadi tidak ada yang perlu
dibersihkan.

---

## 9. Setelah menambah migrasi 0003

Migrasi `0003_watcher_from_addr.sql` memberi `watcher_rw` hak `SELECT` pada
**satu kolom** `executions.from_addr`, supaya Watcher bisa menolak start kalau
`EXECUTOR_WALLET_ADDRESS` menunjuk wallet yang salah.

**Jalankan `guard db migrate` sebelum `guard watch`.**

```bash
guard db migrate
guard db status
```

`guard db status` harus menampilkan `0003` sebagai terapan. Kalau belum,
`guard watch` akan gagal dengan `permission denied for table executions` —
bukan dengan pesan yang menyebut migrasi, karena yang menolak adalah Postgres.

### Mengapa migrasinya manual

Tidak ada proses runtime yang menerapkan migrasi sendiri, dan itu disengaja.
`guard serve` hanya memeriksa **keberadaan tabel** yang dibutuhkannya, bukan
versi skemanya: role `guard_app` tidak punya hak baca ke `schema_migrations`
(RR-12 di Security v1.1, dan bagian 3.2 Arsitektur). Jadi Guard yang berjalan
di atas skema lama tidak akan mengeluh — ia hanya akan gagal di tempat lain,
nanti.

Konsekuensinya untuk operator: **setiap kali repo diperbarui, jalankan
`guard db migrate` lebih dulu.** Tidak ada yang akan mengingatkan.

`guard db migrate` dijalankan sebagai role **migrator**, yang URL-nya ada di
`.env.migrator` — bukan `.env`. Role itu pemilik semua objek; ia tidak dipakai
proses runtime mana pun.

### Verifikasi

```bash
guard db status
```

Keluarannya menyebut `0003 watcher_from_addr` sebagai terapan. Untuk memeriksa
hak yang sesungguhnya diberikan:

```bash
docker compose exec postgres psql -U guard_admin -d payment_guard -c \
  "SELECT column_name, privilege_type FROM information_schema.column_privileges
   WHERE grantee = 'watcher_rw' AND table_name = 'executions'"
```

Harus tepat satu baris: `from_addr | SELECT`. Lebih dari itu berarti ada yang
memberi hak di luar migrasi, dan tes `test_select_columns_match_matrix` akan
menangkapnya juga.

Hak **tingkat tabel** harus tetap tidak ada:

```bash
docker compose exec postgres psql -U guard_admin -d payment_guard -c \
  "SELECT has_table_privilege('watcher_rw', 'executions', 'SELECT')"
```

Harus `f`. Kalau `t`, seseorang mengganti `GRANT SELECT (from_addr)` menjadi
`GRANT SELECT` — yang membuat Watcher bisa membaca seluruh `executions`,
termasuk `decision_id`, `amount_base`, dan `raw_tx`.

### Kalau penjaganya berbunyi

Watcher menolak start dengan pesan yang menyebut dua alamat: yang diharapkan
(dari `.env`) dan yang ditemukan (dari `executions`). Keduanya publik.

1. Periksa `EXECUTOR_WALLET_ADDRESS` di `.env` terhadap field `address` di
   berkas kunci Executor. **Jangan** menampilkan `private_key`.
2. Kalau `.env` yang salah: perbaiki, lalu jalankan Watcher lagi.
3. Kalau `executions` memuat alamat dari wallet lain, wallet Executor pernah
   diganti. Itu bukan sesuatu yang Watcher bisa perbaiki: satu Watcher
   memantau satu wallet. Jalankan Watcher untuk wallet yang sekarang, dan
   catat bahwa pembayaran dari wallet lama tidak akan terpantau.
4. Kejadiannya ada di `audit_log` sebagai `alarm_watcher_wallet_mismatch`,
   lengkap dengan kedua alamat. Baris itu ditulis sebelum Watcher keluar.

---

## 10. Checklist sebelum pembayaran pertama

Penjaga alamat wallet di bagian 9 membandingkan `EXECUTOR_WALLET_ADDRESS`
dengan `from_addr` di `executions`. **Sebelum pembayaran pertama, tabel itu
kosong**, jadi tidak ada apa pun untuk dibandingkan dan penjaganya melewatkan
pemeriksaan — dengan sengaja: menolak start di sana berarti Watcher tidak bisa
dijalankan sebelum pembayaran pertama, padahal justru pembayaran pertama yang
ingin dilihatnya.

Celah itu ditutup oleh checklist ini, bukan oleh kode. Lakukan sekali, sebelum
pembayaran pertama.

### a. Catat alamat wallet Executor

```bash
guard wallet
```

Keluarannya satu baris:

```
wallet 0x<alamat-wallet-executor>
```

Alamat itu **diturunkan dari berkas kunci** Executor, bukan dari `.env`. Catat
apa adanya.

`guard wallet` hanya membaca satu berkas: tanpa koneksi database, tanpa
jaringan, dan tanpa transaksi yang dibangun maupun disiarkan. Ia tidak punya
argumen yang bisa mengubah itu.

> **Jangan memakai `guard execute` untuk membaca alamat.** Laporan startup-nya
> juga mencetak baris `wallet …`, dan versi awal checklist ini memang
> menyuruh begitu — itu keliru. `guard execute` **menyiarkan transaksi** bila
> seluruh ceknya lolos, dan ia tidak punya mode baca-saja. Memakainya untuk
> membaca satu alamat berarti mempertaruhkan pembayaran sungguhan pada harapan
> bahwa salah satu cek akan menolak.

### b. Catat alamat yang dipantau Watcher

```bash
guard watch --cycles 1 --from-block <blok-awal>
```

Laporan startup-nya memuat:

```
watcher siap: chain 84532, USDC 0x<alamat-usdc> (6 desimal),
memantau transfer dari 0x<alamat-yang-dipantau>
```

Alamat itu dibaca dari **`EXECUTOR_WALLET_ADDRESS` di `.env`**. Dua sumber yang
berbeda — berkas kunci dan konfigurasi — dan itulah yang membuat
perbandingannya berarti.

Blok awal diambil dari bagian 8.

### c. Bandingkan ketiganya

Bandingkan **tanpa peka huruf besar/kecil**: explorer menampilkan alamat
ber-checksum EIP-55, `.env` mungkin memuat bentuk itu, dan sistem menyimpannya
lowercase. Ketiganya harus menunjuk alamat yang sama:

1. yang dicetak `guard wallet` (dari berkas kunci),
2. yang dicetak `guard watch` (dari `.env`),
3. nilai `EXECUTOR_WALLET_ADDRESS` di `.env`.

Untuk membandingkan tanpa menyalin dengan mata:

```bash
# Ketik ulang kedua alamat dari keluaran di atas.
a=$(printf '%s' '<alamat-dari-guard-wallet>' | tr 'A-F' 'a-f')
b=$(printf '%s' '<alamat-dari-guard-watch>'   | tr 'A-F' 'a-f')
[ "$a" = "$b" ] && echo COCOK || echo "TIDAK COCOK"
```

### d. Kalau tidak sama: JANGAN lanjut

Perbaiki `EXECUTOR_WALLET_ADDRESS` di `.env` lebih dulu, lalu ulangi dari (a).
Jangan mengirim pembayaran sambil berpikir akan diperbaiki nanti: pembayaran
yang dikirim sebelum alamatnya benar tidak akan terpantau, dan Watcher tidak
bisa memundurkan kursornya untuk menemukannya (bagian 8.4).

Alamat yang perlu diperbaiki selalu yang di `.env`. Yang **tidak** boleh
diubah adalah berkas kunci — alamatnya diturunkan dari kuncinya, dan
mengubahnya berarti wallet lain.

### e. Kalau sama: boleh lanjut

Lanjutkan ke pembayaran pertama.

### f. Setelah pembayaran pertama

Checklist ini tidak perlu diulang. Dua lapis mengambil alih:

- **Penjaga saat start** (bagian 9) akan menolak alamat yang salah, karena
  `executions` sudah punya baris untuk dibandingkan.
- **Reconciler** menandai `executed_not_settled` bila pembayaran yang sudah
  disiarkan tidak pernah punya settlement `final` setelah batas waktunya —
  yang persis gejalanya kalau Watcher memantau wallet yang salah.

Lapis kedua itu melaporkan belakangan, bukan mencegah. Itu sebabnya checklist
ini ada.
