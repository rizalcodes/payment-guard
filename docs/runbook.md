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

Executor mengambil nonce dari yang lebih besar antara dua sumber:
`eth_getTransactionCount(pending)` dan `max(nonce)` dari `executions` untuk
alamat itu. Jadi node yang baru di-restart tidak bisa membuat nonce terpakai
dua kali.

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
