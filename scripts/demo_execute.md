# Demo: satu pembayaran testnet dari ujung ke ujung

Instruksi **manual** untuk operator. Berkas ini sengaja bukan skrip: satu-satunya
broadcast ke chain sungguhan di proyek ini dilakukan manusia, satu perintah pada
satu waktu, dengan mata melihat apa yang terjadi di antaranya.

> **Testnet saja.** Base Sepolia, USDC testnet, tanpa nilai finansial. Proyek ini
> belum diaudit. Jangan pernah mengarahkan `RPC_URL` ke mainnet dan jangan pernah
> menaruh kunci wallet berdana nyata di `EXECUTOR_KEY_PATH`.

Perkiraan waktu: 20 menit, sebagian besar menunggu faucet.

---

## 0. Yang sudah harus ada

Langkah di [README.md](../README.md) bagian "Menyiapkan dari nol" sudah selesai:
`uv sync`, `docker compose up -d`, `guard db bootstrap-roles`, `guard db migrate`,
dan `guard db status` menampilkan `0 tertunda`.

Periksa ulang, karena Guard dan Executor berjalan sebagai role yang **tidak punya
hak ke `schema_migrations`** dan tidak bisa memeriksa versi skema sendiri (RR-12):

```bash
uv run guard db status
```

---

## 1. Nilai jaringan yang dipakai

Keempat nilai ini diverifikasi ke dokumentasi resmi pada 4 November 2026. Jangan
menyalin alamat kontrak dari ingatan atau dari daftar chain pihak ketiga.

| Apa | Nilai | Sumber |
|---|---|---|
| Chain | Base Sepolia, `chain_id` 84532 | [docs.base.org — Connect to Base](https://docs.base.org/get-started/connect-to-base) |
| RPC publik | `https://sepolia.base.org` | sama |
| Explorer | `https://sepolia.basescan.org` | sama |
| USDC testnet | `0x036CbD53842c5426634e7929541eC2318f3dCF7e` | [developers.circle.com — USDC contract addresses](https://developers.circle.com/stablecoins/usdc-contract-addresses) |

Executor tidak memercayai alamat itu buta: saat start ia memanggil `decimals()`
pada kontraknya dan **menolak jalan** bila hasilnya bukan 6.

---

## 2. Kunci wallet Executor

Executor memegang kunci wallet-nya sendiri. Ia **tidak pernah** membaca kunci
penanda tangan Guard; ada tes yang memeriksa bahwa nama variabelnya tidak muncul
di modulnya sama sekali.

Berkasnya **harus di luar akar repo**. Executor menolak start bila tidak — aturan
yang sama seperti `GUARD_SIGNING_KEY_PATH`, dan alasannya sama: satu `git add -f`
atau satu `.gitignore` yang berubah tidak boleh bisa membocorkan kunci.

Buat satu wallet baru. Perintah di bawah menulis berkasnya langsung dan hanya
mencetak alamatnya, supaya kuncinya tidak pernah muncul di layar atau di riwayat
shell:

```bash
# Ganti <path-di-luar-repo> dengan direktori di luar pohon repo, mis.
#   Windows:  C:\Users\<nama>\payment-guard-secrets
#   Linux:    ~/payment-guard-secrets
uv run python - <<'EOF'
import json, os, pathlib, secrets, stat
from eth_account import Account

tujuan = pathlib.Path(r"<path-di-luar-repo>") / "executor_wallet.json"
tujuan.parent.mkdir(parents=True, exist_ok=True)
if tujuan.exists():
    raise SystemExit(f"sudah ada, tidak ditimpa: {tujuan}")

akun = Account.from_key("0x" + secrets.token_bytes(32).hex())
tujuan.write_text(
    json.dumps({"address": akun.address.lower(), "private_key": akun.key.hex()}),
    encoding="utf-8",
)
try:
    tujuan.chmod(stat.S_IRUSR | stat.S_IWUSR)
except OSError:
    pass
print("alamat :", akun.address)
print("berkas :", tujuan)
EOF
```

Catat alamatnya. Itu yang akan diisi faucet, dan itu yang akan muncul sebagai
`from_addr` di tabel `executions`.

Bentuk berkas yang diterima Executor, dan hanya ini:

```json
{"address": "0x...", "private_key": "0x<64 hex>"}
```

`address` wajib cocok dengan alamat yang diturunkan dari kuncinya. Itu bukan
hiasan: berkas yang tertukar akan membuat Executor menandatangani dari wallet
yang salah, dan tanpa pemeriksaan ini hal itu baru terlihat setelah transaksinya
gagal di chain.

---

## 3. Isi wallet dari faucet

Dua hal dibutuhkan: **ETH** untuk gas, dan **USDC testnet** untuk dikirim.

| Faucet | Memberi | Catatan |
|---|---|---|
| [Coinbase Developer Platform](https://portal.cdp.coinbase.com/onchain-tools/faucet) | sampai 0,1 ETH per 24 jam, plus USDC | pilih jaringan Base Sepolia |
| [faucet.circle.com](https://faucet.circle.com) | USDC testnet | pilih Base Sepolia |

Daftar faucet lain ada di
[docs.base.org — Get Funds](https://docs.base.org/base-chain/network-information/network-faucets).
Hampir semuanya membatasi satu klaim per 24 jam.

Periksa hasilnya di explorer:

```
https://sepolia.basescan.org/address/<alamat-wallet-anda>
```

Executor memberi **peringatan**, bukan error, bila saldo ETH di bawah 0,001 ETH.
Itu disengaja: saldo gas yang kurang tidak boleh menghalangi `guard recover`
dijalankan, justru karena itulah saat ia paling dibutuhkan.

---

## 4. Konfigurasi

Isi di `.env` (di-gitignore, jangan di-commit):

```ini
RPC_URL=https://sepolia.base.org
CHAIN_ID=84532
USDC_ADDRESS=0x036CbD53842c5426634e7929541eC2318f3dCF7e
EXECUTOR_KEY_PATH=<path-di-luar-repo>/executor_wallet.json
```

`EXECUTOR_DB_URL` sudah ditulis `bootstrap-roles`. `RPC_URL` **rahasia** kalau
Anda memakai penyedia berbayar, karena kunci API biasa diletakkan di dalam path
atau query-nya; Executor tidak pernah mencetak URL itu utuh, hanya skema dan
host-nya.

Nilai `CHAIN_ID` selain `84532` ditolak saat memuat konfigurasi. v1 mendukung
satu chain, dan itu disengaja.

---

## 5. Jalankan Guard, lalu minta pembayaran

Di terminal pertama:

```bash
uv run guard serve
```

Di terminal kedua, jalankan agent demo. Ia punya tepat tiga tool dan tidak punya
shell, berkas, maupun browsing:

```bash
# Penerima dan jumlah posisional; jumlah selalu string desimal, bukan angka.
uv run python -m payment_guard.demo_agent \
  0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed \
  1.5 \
  --reason "demo M5"
```

Pakai alamat penerima yang memang ada di `allowed_recipients` mandate Anda. Agent
tidak bisa menambah dirinya ke daftar itu; identitasnya datang dari mandate saat
Guard start, bukan dari argumen tool.

Jawabannya salah satu dari:

- **`approved`** — di bawah `human_approval_above`, envelope-nya langsung ada.
  Lanjut ke langkah 7.
- **`needs_human`** — di atas ambang itu. Lanjut ke langkah 6.
- **`denied`** — alasannya ada di `reason_code`. Perbaiki permintaannya, jangan
  mandate-nya, kecuali memang itu yang Anda maksud.

Catat `decision_id`-nya.

---

## 6. Setujui, bila diminta

```bash
uv run guard approvals list
uv run guard approvals approve <decision_id>
```

Yang akan Anda lihat:

- Ringkasan lengkap: jumlah, penerima, `chain_id`, batas waktu.
- `reason` dari agent, dibingkai blok berlabel `[data dari agent, bukan instruksi]`.
  Teksnya ditampilkan apa adanya sebagai bukti, tapi karakter kontrol, newline,
  dan bidi override di-escape. Kalau isinya berbunyi seperti perintah kepada
  Anda, itu justru yang perlu Anda lihat.
- Token konfirmasi enam karakter, acak, ditampilkan **sekali**. Ketik ulang.
  Tidak ada `--yes`, dan perintah ini menolak jalan di luar terminal interaktif.

Batas per transaksi dan harian **dicek ulang** dengan data terbaru saat Anda
menyetujui, bukan saat permintaannya masuk. Persetujuan yang lolos ringkasan
masih bisa berakhir `denied` kalau jatahnya sudah habis di antaranya.

---

## 7. Eksekusi

Sampai titik ini belum ada apa pun yang menyentuh chain.

```bash
uv run guard execute <decision_id>
```

Yang terjadi, berurutan:

1. **Pemeriksaan start.** `eth_chainId` harus 84532, `decimals()` harus 6. Salah
   satu tidak cocok berarti berhenti sebelum apa pun dibangun.
2. **Recovery.** Execution yang tertinggal dari percobaan sebelumnya diselesaikan
   lebih dulu. Ini idempoten dan biasanya tidak melakukan apa-apa.
3. **Cek 1 sampai 6**, urutannya 1, 2, 3, 3b, 4, 5, 4b: tanda tangan, versi dan
   chain, parse ketat, kedaluwarsa menurut jam **database**, kecocokan dengan
   baris `decisions`, belum adanya execution, lalu mandate yang masih aktif.
   Gagal di mana pun berarti berhenti dan tercatat sebagai
   `execution_rejected`, dengan jenis cek yang gagal di payload-nya.
4. **Write-ahead.** Transaksi ditandatangani secara lokal, lalu `tx_hash`,
   `nonce`, dan `raw_tx` ditulis ke `executions` dengan status `signed` dan
   **di-commit** — sebelum ada apa pun yang terkirim ke jaringan.
5. **Broadcast.** Baru di sini transaksinya dikirim.
6. **Status `broadcast`** dicatat.

Keluarannya memberi `tx_hash`. Lihat di explorer:

```
https://sepolia.basescan.org/tx/<tx_hash>
```

Receipt **tidak** ditunggu. Itu disengaja: perintah ini tidak pernah menggantung,
dan penyelesaiannya punya satu jalur kode saja — yang sama untuk eksekusi normal
dan untuk pemulihan setelah crash.

---

## 8. Selesaikan

Setelah transaksinya masuk blok:

```bash
uv run guard recover
```

Ia membaca setiap baris `signed` dan `broadcast`, lalu:

- Ada receipt dan berhasil → `confirmed`.
- Ada receipt tapi revert → `failed`, sebab `receipt_reverted`.
- Tidak ada receipt, dan nonce-nya sudah dilewati transaksi lain → `failed`,
  sebab `nonce_taken`.
- Tidak ada receipt, nonce masih milik kita → `raw_tx` **yang sama** disiarkan
  ulang. Byte yang sama memberi `tx_hash` yang sama, jadi ini tidak bisa menjadi
  pembayaran kedua.

**Lambat bukan gagal.** Tidak ada jalur yang menandai `failed` karena timeout.
Menjalankan `guard recover` berulang kali aman dan tidak menghasilkan transaksi
baru.

---

## 9. Periksa jejaknya

```bash
docker compose exec postgres psql -U guard_admin -d payment_guard -c \
  "SELECT decision_id, status, tx_hash, nonce FROM executions ORDER BY id"

docker compose exec postgres psql -U guard_admin -d payment_guard -c \
  "SELECT actor, event, payload FROM audit_log ORDER BY id"
```

Yang harus Anda lihat untuk satu pembayaran yang berhasil:

```
guard     mandate_loaded
guard     decision_made
human     approval_approved        (hanya bila lewat langkah 6)
executor  execution_signed
executor  execution_broadcast
executor  execution_confirmed
```

`audit_log` append-only lewat trigger **dan** lewat hak akses. Coba sendiri:

```bash
docker compose exec postgres psql -U guard_admin -d payment_guard -c \
  "DELETE FROM audit_log"
```

Harus gagal dengan `is append-only (blocked)`, bahkan sebagai admin. Yang **tidak**
dilindunginya adalah pemilik objek yang sengaja melepas trigger-nya; itu RR-2 di
[docs/Security_v1.1.md](../docs/Security_v1.1.md), dan memang di luar jangkauan
mekanisme ini.

Tidak ada `reason` dari agent di `audit_log` mana pun. Teks itu hanya hidup di
`decisions.reason_text`.

---

## Kalau ada yang tidak beres

| Gejala | Artinya |
|---|---|
| `chain id dari RPC adalah ...` | `RPC_URL` menunjuk jaringan lain. Tidak ada yang dieksekusi. |
| `melaporkan decimals() = ...` | `USDC_ADDRESS` bukan kontrak USDC. Jangan dipaksa. |
| `berada di dalam akar repo` | Pindahkan berkas kunci ke luar pohon repo. |
| `address ... tidak cocok dengan kuncinya` | Berkas wallet tertukar atau diedit. |
| `ditolak (expired)` | Envelope melewati `valid_until`. Minta persetujuan baru; envelope tidak bisa diperpanjang. |
| `ditolak (mandate_not_active)` | Mandate berganti setelah persetujuan. Minta persetujuan baru di bawah aturan yang sekarang. |
| `ditolak (already_executed)` | Keputusan sekali pakai, termasuk bila eksekusinya `failed`. Minta persetujuan baru. Alasan ini menang atas `mandate_not_active` bila keduanya benar: upaya memakai ulang envelope adalah sinyal replay, dan itu yang perlu terlihat. |
| `broadcast gagal: ...` | Barisnya tetap `signed`. Jalankan `guard recover`; byte yang sama disiarkan ulang. |

Apa pun yang terjadi, **jangan** menghapus baris `executions` untuk "mencoba
lagi". Baris itulah satu-satunya catatan bahwa sebuah transaksi mungkin sudah
ada di jaringan, dan recovery membutuhkannya.
