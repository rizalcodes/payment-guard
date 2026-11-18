<!-- Catatan bukti satu kejadian yang sudah lewat. Angka dan ID diambil dari keluaran mentah, bukan dari ingatan. -->

# Demo MCP di devnet lokal

Catatan apa yang terbukti oleh satu demo yang dijalankan 18 November 2026,
apa yang **tidak** terbukti, dan bagaimana pembaca bisa memeriksanya sendiri.

## Ringkasan

Agent demo menjalankan `guard serve` sebagai subproses stdio dan dua keputusan
masuk lewat jalur MCP itu, bukan lewat skrip yang memanggil
`decision_service.check_payment` langsung. Kedua tool Guard terpanggil —
`check_payment` dan `get_decision_status` — satu keputusan lolos aturan secara
otomatis, satu lagi berhenti di `needs_human` lalu disetujui pemilik repo
dengan `guard approvals approve` yang diketik di terminal sungguhan. Envelope
devnet, yang ber-`chain_id` 31337, terverifikasi di kedua jalur.

## Yang TIDAK dibuktikan

Bagian ini sengaja di depan, bukan di catatan kaki.

**Tidak ada broadcast ke chain.** Anvil mati selama seluruh demo, dan
`execute_payment` di agent demo masih stub M4: ia memverifikasi envelope
dengan kunci publik lalu berhenti. `broadcast` bernilai `false` di kedua
skenario. Jadi yang terbukti adalah jalur MCP sampai verifikasi envelope,
**bukan** sampai uang bergerak.

**Tidak ada wallet yang dibaca.** Stub itu tidak menyentuh kunci wallet dan
tidak menghubungi RPC. Guard sendiri tidak pernah memegang kunci wallet, jadi
batas ini bukan kekurangan pada Guard — hanya batas pada apa yang demo ini
tunjukkan.

**Pembacaan status dan verifikasi envelope tidak meninggalkan jejak audit.**
`get_decision_status` hanya membaca, dan `execute_payment` berjalan di host
agent tanpa menyentuh database. Akibatnya, dari `audit_log` saja tidak ada
cara mengetahui bahwa agent pernah membaca sebuah keputusan atau
memverifikasi envelope-nya. Itu konsisten dengan desainnya dan bukan bug,
tapi harus tertulis: jejak audit di repo ini mencatat **keputusan dan
persetujuan**, bukan pembacaan.

## Dua keputusan

| `decision_id` | nominal | status akhir | `reason_code` | jalur |
|---|---|---|---|---|
| `34770f00-bd91-4cc7-9eb5-44714b4c9d93` | 10 tUSDC | `approved` | `approved` | otomatis, lolos semua aturan |
| `75184603-eeaf-4baa-912f-fcb70dc9eb06` | 40 tUSDC | `approved` | `approved_by_human` | `needs_human` lalu disetujui manusia lewat `guard approvals approve` |

Nominal dalam base unit: `10000000` dan `40000000`. Keduanya ke penerima yang
sama, `0x3c44cdddb6a900fa2b585dd299e03d12fa4293bc`, satu-satunya alamat di
`allowed_recipients` mandate devnet. Yang membedakan keduanya hanyalah
`human_approval_above` yang bernilai `25000000`.

Kedua envelope ditandatangani `key_id` `guard-2026-11-cb0a` dengan `chain_id`
`31337`.

Untuk keputusan kedua, dua jendela waktu yang berbeda tercatat dan tidak boleh
dicampur: `expires_at` `1794984024` adalah batas menunggu manusia, sedangkan
`valid_until` `1794981464` adalah batas eksekusi yang dihitung dari detik
persetujuan. Yang kedua lebih awal daripada yang pertama.

Sumber: `m9-A-10tusdc.txt`, `m9-B-40tusdc.txt`, dan
`m9-B-sesudah-approve.txt` di direktori scratchpad sesi itu.

### Bukti jalur MCP memang dipakai

Server mencatat permintaan protokolnya. Tiga sesi `guard serve` terjadi, dan
jumlah permintaannya berbeda sesuai apa yang dipanggil:

| sesi | `ListToolsRequest` | `CallToolRequest` |
|---|---|---|
| skenario pertama, 10 tUSDC | 1 | 2 |
| skenario kedua, 40 tUSDC | 1 | 2 |
| pembacaan status sesudah persetujuan | 1 | 1 |

Dua yang pertama memanggil `check_payment` lalu `get_decision_status`. Yang
ketiga **hanya** `get_decision_status`, dengan sengaja: memanggil
`check_payment` lagi akan membuat keputusan baru alih-alih membaca yang sudah
disetujui.

Agent juga menolak jalan bila server menawarkan nama tool di luar dua itu —
`demo_agent.py` baris 220–222 membandingkan hasil `list_tools()` dengan
`GUARD_TOOL_NAMES` dan melempar `DemoAgentError` bila berbeda. Jadi permukaan
yang dipakai bukan sekadar yang kebetulan dipanggil.

## Jejak audit

Dari `m9-06-audit.txt`, apa adanya. Tiga baris audit untuk kedua keputusan.

**Keputusan pertama — satu event:**

```
event : decision_made    actor : guard
```

**Keputusan kedua — dua event:**

```
event : decision_made       actor : guard    (status needs_human)
event : approval_approved   actor : human    (guard_running "false")
```

Tiga hal yang perlu dibaca dari itu:

**Tidak ada event `created` yang terpisah.** Pembuatan baris `decisions` dan
keputusannya ditulis sebagai **satu** kejadian, `decision_made`, dengan status
akhirnya ada di dalam payload. Siapa pun yang mencari pasangan
"created lalu approved" tidak akan menemukannya.

**Nama event persetujuan adalah `approval_approved`**, beraktor `human`.

**`guard_running` bernilai `"false"` saat persetujuan**, dan itu benar:
`guard serve` hanya hidup selama sesi MCP agent, lalu mati. Persetujuan
terjadi di luar masa hidup sesi Guard. Nilainya string, bukan boolean.

Selain ketiga baris itu, ada **dua** baris `mandate_loaded` beraktor `guard`
tanpa `decision_id` — satu per sesi `guard serve` yang dinyalakan agent. Tidak
ada kejadian lain yang tidak terduga.

## Catatan teknis: dua hal yang ditemukan dan diperbaiki

Demo ini tidak berjalan di percobaan pertama. Dua cacat di agent demo
ditemukan dan diperbaiki lebih dulu; keduanya hanya menyangkut
`src/payment_guard/demo_agent.py`.

**`--network` tidak pernah diteruskan ke verifikasi** (`fd8b44a`).
`execute_payment` memanggil `verify(envelope, registry)` tanpa argumen
jaringan, sehingga `verify` memakai bawaannya sendiri dan selalu menuntut
`chain_id` 84532. Envelope devnet ber-`chain_id` 31337, jadi agent **selalu**
menolaknya, dan tidak ada flag untuk memberi tahu agent bahwa ia sedang di
devnet.

**Bawaan `--chain` dan `--public-keys-file` keduanya produksi** (`75935c0`).
`--chain` bawaannya `base-sepolia`, yang tidak sah di jaringan devnet.
`--public-keys-file` bawaannya `guard_public.json` di akar repo — **registri
produksi**. Berkas itu ada, jadi memakainya di devnet tidak gagal karena
berkasnya hilang melainkan karena `key_id` envelope devnet tidak ada di
dalamnya. Sekarang `--chain` diturunkan dari `--network`, dan
`--public-keys-file` wajib di luar jaringan produksi.

Kedua perbaikan itu diuji lewat tes unit dan lewat demo yang dicatat dokumen
ini. Tidak ada jalur lain yang mengujinya.

## Cara memeriksa ulang

Prasyarat: `payment-guard-postgres-devnet` hidup. Anvil **tidak** diperlukan,
karena tidak ada langkah yang menyentuh chain.

Path registri kunci publik devnet ada di `GUARD_PUBLIC_KEYS_PATH` pada berkas
env devnet Anda; agent tidak membaca berkas env, jadi nilainya harus diberikan
lewat flag.

**1. Buat keputusan lewat jalur MCP.** Nominal di bawah
`human_approval_above` memberi `approved`, di atasnya memberi `needs_human`:

```bash
python -m payment_guard.demo_agent <recipient> 10 \
  --env-file .env.devnet \
  --public-keys-file <GUARD_PUBLIC_KEYS_PATH> \
  --chain devnet-local --network devnet --reason "demo"
```

**2. Setujui, diketik manusia.** Tidak ada `--yes` dan tidak ada mode
non-interaktif; perintah ini menolak stdin yang bukan terminal dan menuntut
token enam karakter diketik ulang:

```bash
guard approvals --env-file .env.devnet approve <decision_id>
```

**3. Baca status lewat MCP tanpa membuat keputusan baru.** Agent demo tidak
bisa dipakai di sini karena satu putarannya selalu dimulai dengan
`check_payment`; yang dibutuhkan adalah sesi MCP yang hanya memanggil
`get_decision_status`, lalu menyerahkan hasilnya ke `execute_payment` dengan
`network="devnet"`. Skrip pembantu untuk itu hidup di scratchpad sesi, bukan
di repo.

**4. Periksa audit, hanya `SELECT`.** Ambil **setiap** baris yang payload-nya
menyebut salah satu `decision_id`, tanpa menyaring ke nama event yang
diharapkan — penyaringan seperti itu tidak akan pernah menemukan kejadian yang
tidak diharapkan:

```sql
SELECT id, ts, actor, event, payload
FROM audit_log
WHERE payload->>'decision_id' IN (%s, %s)
ORDER BY id;
```

Setel `connect_timeout=10` pada koneksinya. Tanpa itu, percobaan terhadap
cluster yang mati menggantung dua menit sebelum gagal, dan kegagalan yang
lambat lebih buruk daripada yang cepat.

## Batas, dan apa yang berikutnya

- **Anvil belum dinyalakan lagi** sejak Docker di mesin itu restart. Database
  devnet masih memuat **6 baris `settlements`** (semuanya `final`, di blok 5,
  18, 31, 44, 70, dan 83) dan **5 baris `executions`** (semuanya `confirmed`,
  nonce 0 sampai 4), dan semuanya merujuk blok serta `tx_hash` dari chain yang
  lama.

  Apakah rujukan itu masih ada di chain **belum diperiksa**, dan tidak bisa
  diperiksa sekarang: RPC tidak bisa dihubungi, jadi tidak ada chain untuk
  dibandingkan. Anvil menyimpan state di memori tanpa volume, sehingga
  *dugaan* yang masuk akal adalah ia akan bangkit sebagai chain baru dari blok
  0 dan rujukan itu tidak akan ditemukan — tapi itu dugaan, bukan temuan.
  Memeriksanya butuh Anvil dinyalakan lebih dulu, dan keputusan itu belum
  diambil.
- **Broadcast belum diuji lewat jalur MCP.** Yang sudah diuji terpisah, di
  luar demo ini, adalah `guard execute` langsung dari baris perintah.
- **Keputusan tentang jejak baca belum diambil.** Apakah pembacaan status
  perlu meninggalkan jejak audit adalah pertanyaan terbuka, bukan cacat yang
  sudah disepakati.
