# Daftar kejadian `audit_log`

Dokumen turunan. Sumber kebenarannya `src/payment_guard/events.py`: enum `Event`,
enum `Actor`, dan peta `EVENT_ACTORS`. Ada tes yang membandingkan daftar di
bawah dengan enum itu (`tests/unit/test_events_doc.py`), jadi keduanya tidak
bisa menyimpang tanpa tes gagal.

Aturan yang berlaku untuk seluruh tabel:

- **Hanya `events.py` yang menulis `audit_log`.** Tidak ada nama kejadian
  sebagai string di luar enum. Keduanya ditegakkan tes AST
  (Arsitektur v1.1 bagian 12).
- **`audit_log` append-only**, lewat trigger *dan* lewat hak akses. Tidak ada
  role yang punya UPDATE atau DELETE. Yang tidak dilindunginya adalah pemilik
  objek yang sengaja melepas trigger-nya: Security v1.1 RR-2.
- **`reason` dari agent tidak pernah masuk payload.** Teks itu hanya hidup di
  `decisions.reason_text`. `reason_code` boleh, karena ia enum tertutup milik
  Guard dan bukan teks dari luar (PRD v2.1 bagian 3 nomor 10, Security v1.1
  T-8, I3).
- **Envelope, tanda tangan, dan token konfirmasi tidak pernah masuk payload.**
  Envelope adalah kredensial sekali pakai (Security v1.1 I5).
- Payload hanya memuat `str`, `int`, `dict`, dan `list`. `float`, `bool`, dan
  `None` ditolak, supaya bentuk kanoniknya tidak ambigu. Karena itu
  `guard_running` ditulis sebagai string `"true"` / `"false"`.

## Kejadian

| Kejadian | Actor | Ditulis saat | Payload |
|---|---|---|---|
| `mandate_loaded` | `guard` | Guard start memuat mandate dan memperbarui `active_mandates` | `mandate_id`, `mandate_hash`, `agent_id` |
| `decision_made` | `guard` | Satu keputusan `check_payment` tercatat | `decision_id`, `status`, `reason_code`, `agent_id`, `recipient`, `amount_base` |
| `repeated_retry` | `guard` | Lebih dari lima `retry_blocked` dalam satu jendela cooldown | `agent_id`, `recipient`, `attempts`, `cooldown_started_at`, `decision_id` |
| `guard_lock_lost` | `guard` | Heartbeat gagal: koneksi lock hilang, Guard berhenti menjawab | `agent_id`, `detail` |
| `startup_check_failed` | `guard` | Pemeriksaan saat start gagal, mis. Guard kedua untuk agent yang sama | `check`, `agent_id`, `detail` |
| `approval_approved` | `human` | Operator menyetujui satu `needs_human`; envelope ditandatangani | `decision_id`, `agent_id`, `recipient`, `amount_base`, `status`, `reason_code`, `guard_running` |
| `approval_rejected` | `human` | Operator menolak, atau cek ulang per_tx/daily gagal saat approve | sama seperti di atas |
| `decision_expired` | `guard` atau `human` | `needs_human` melewati `expires_at` dan menjadi `expired` | sama seperti di atas |
| `execution_signed` | `executor` | Transaksi ditandatangani dan ditulis write-ahead, belum disiarkan | `decision_id`, `agent_id`, `tx_hash`, `nonce`, `from_addr`, `to_addr`, `recipient`, `amount_base`, `chain_id` |
| `execution_broadcast` | `executor` | `eth_sendRawTransaction` diterima node | `decision_id`, `tx_hash`, `nonce`, dan `agent_id` bila diketahui |
| `execution_confirmed` | `executor` | Receipt diterima dengan `status = 1` | `decision_id`, `tx_hash`, `nonce`, `block_number` |
| `execution_failed` | `executor` | Receipt revert, atau nonce dipakai transaksi lain. **Bukan** timeout | `decision_id`, `tx_hash`, `nonce`, `cause`, dan `block_number` bila ada receipt |
| `execution_rejected` | `executor` | Salah satu cek Executor gagal; tidak ada transaksi yang dibangun | `decision_id`, `check` |
| `settlement_finalized` | `watcher` | Melewati kedalaman konfirmasi dan hash bloknya masih kanonik | `settlement_id`, `tx_hash`, `log_index`, `block_number`, `block_hash` |
| `settlement_moved` | `watcher` | Hash blok berubah tapi transaksinya masih ada: baris yang sama dipindah | sama seperti di atas, plus `new_block_number` |
| `settlement_reorged` | `watcher` | Hash blok berubah **dan** transaksinya hilang. Baris tidak dihapus | sama seperti di atas, plus `previous_status` |
| `settlement_restored` | `watcher` | Transaksi yang sudah `reorged` muncul lagi di blok lain | sama seperti di atas, plus `new_block_number` |
| `alarm_final_settlement_reorged` | `watcher` | Baris yang sudah `final` ter-reorg di dalam jendela mundur K. **Alarm** | sama seperti di atas, plus `previous_status` |
| `reconciliation_state_changed` | `reconciler` | State rekonsiliasi satu subjek berubah | `subject_type`, `subject_id`, `state`, `detail`, dan `previous_state` bila ada |

## Catatan per kelompok

### Kejadian persetujuan memakai actor `human`, bukan `approver`

Yang menekan tombol adalah operator, dan Approvals CLI hanyalah alatnya.
Memakai `approver` akan menyamarkannya menjadi tindakan proses, padahal nilai
bukti terbesarnya justru "ada manusia yang memutuskan" — Security v1.1 R-a,
sangkalan operator.

### `decision_expired` menerima dua actor

Kedaluwarsa bersifat lazy (Arsitektur v1.1 bagian 7): siapa pun yang pertama
menyentuh baris itu yang menuliskannya. `guard` dan `approver` adalah dua role
yang punya hak UPDATE kolom status di `0002_roles.sql`, jadi keduanya sah.
Sampai M5 hanya jalur `human` yang dipakai.

### Kejadian eksekusi semuanya `executor`

Di sini memang prosesnya yang bertindak: tidak ada manusia di antara tanda
tangan dan broadcast, dan justru itu yang membuat write-ahead perlu.
`execution_failed` juga tidak pernah `human` — ia menyatakan apa yang dilihat
Executor di chain, bukan penilaian siapa pun.

`payload["cause"]` pada `execution_failed` berasal dari daftar tertutup
`payment_guard.executor.FAILURE_CAUSES`, dan isinya tepat dua:

- `receipt_reverted` — transaksi masuk blok tapi gagal dijalankan.
- `nonce_taken` — nonce-nya sudah dilewati transaksi lain, dan tidak ada
  receipt untuk transaksi kita.

Tidak ada sebab `timeout`, dan itu disengaja: menandai `failed` karena lambat
akan menyembunyikan pembayaran yang sesungguhnya berhasil. Baris yang lambat
tetap `signed` atau `broadcast` sampai chain menjawab (PRD v2.1 bagian 9).

### `execution_rejected`, dan mengapa jenis cek ada di payload

Satu nama kejadian untuk semua penolakan cek, dengan jenisnya di
`payload["check"]`, dari daftar tertutup
`payment_guard.executor.REJECTION_REASONS`:

`signature_invalid`, `version_or_chain_mismatch`, `not_approved`, `expired`,
`decision_mismatch`, `already_executed`, `mandate_not_active`.

Nama kejadian yang mengkodekan jenis cek akan berarti satu nama baru setiap
kali daftar cek bertambah, dan enum `Event` berhenti bisa dipakai sebagai
daftar. Keputusan operator, 4 November 2026.

`payload` **tidak** memuat detail penolakan. Detail itu memuat nilai yang
datang dari envelope — alamat, jumlah, hash — dan payload audit tidak menerima
teks yang asalnya dari luar. Detailnya ditampilkan ke operator di stdout.

### Urutan cek, dan mengapa `already_executed` menang

Daftar cek Executor berjalan 1, 2, 3, 3b, 4, 5, 4b. Cek 5 (sudah ada
execution?) mendahului cek 4b (mandate masih aktif?) dengan sengaja.

Ketika sebuah keputusan sudah dieksekusi **dan** mandate-nya sudah berganti,
keduanya benar. Yang harus tercatat adalah `already_executed`: upaya memakai
ulang envelope yang sudah terpakai adalah sinyal replay. Dengan urutan
terbalik, satu pergantian mandate sesudah eksekusi akan membuat setiap upaya
replay sesudahnya tercatat sebagai `mandate_not_active`, dan sinyal pemakaian
ulang kredensial hilang di balik keluhan tentang aturan.

Himpunan envelope yang **lolos** sama untuk kedua urutan; yang berbeda hanya
alasan yang dilaporkan. Keputusan operator, 4 November 2026.

### Kejadian Watcher semuanya `watcher`, dan tidak ada untuk `pending`

Lima kejadian `settlement_*` dan `alarm_*` hanya bisa ditulis `watcher`.
Reconciler membaca hasilnya tapi tidak boleh menuliskannya: satu penulis per
fakta (Arsitektur v1.1 prinsip 2). Tidak ada manusia di jalur ini sama sekali
— Watcher melaporkan apa yang dilihatnya di chain.

Yang sengaja **tidak** ada adalah kejadian untuk INSERT settlement `pending`.
Arsitektur bagian 10 menuliskan langkah itu tanpa audit, dan memang
seharusnya: setiap transfer dari wallet Executor menghasilkan satu baris, jadi
mencatatnya lagi di `audit_log` hanya menduplikasi `settlements` dengan tabel
yang tidak bisa di-UPDATE. Yang perlu jejak adalah **perubahan** keadaan.

Nama-nama itu diambil apa adanya dari tabel state machine di PRD v2 bagian 10
dan diagram sequence Arsitektur bagian 10. Tidak ada nama baru yang dikarang
di M6.

### `reconciliation_state_changed`, dan mengapa state ada di payload

Satu nama kejadian untuk semua state, dengan state-nya di `payload["state"]`,
dari daftar tertutup `payment_guard.reconciler.RECONCILIATION_STATES`:

`matched`, `unmatched_onchain`, `mismatch`, `executed_not_settled`, `expired`,
`invalidated`.

Alasannya sama dengan `execution_rejected`: nama yang mengkodekan state akan
berarti satu nama baru setiap kali daftar state bertambah, dan enum `Event`
berhenti bisa dipakai sebagai daftar. Arsitektur bagian 11 juga sudah
menuliskannya begitu.

Baris audit hanya ditulis ketika state-nya **berubah**. `guard reconcile`
dijalankan berulang, dan `audit_log` yang dipenuhi "masih cocok" setiap lima
menit berhenti bisa dibaca sebagai bukti.

### Payload settlement memuat identitas on-chain, bukan jumlah

`settlement_*` mencatat `tx_hash`, `log_index`, `block_number`, dan
`block_hash` — yaitu hal yang bisa dibuka orang lain di explorer dan
diperiksa ulang. Jumlahnya tidak ikut: ia ada di `settlements.amount_base`,
dan menyalinnya ke audit berarti dua tempat yang bisa berbeda.

`reconciliation_state_changed` adalah pengecualiannya: `detail`-nya memuat
jumlah yang **diharapkan** dan yang **terlihat** ketika keduanya berbeda,
karena di situlah seluruh isi temuannya. Keduanya ditulis sebagai string,
bukan `int`: base units bisa melampaui 2^53, dan payload audit tidak pernah
melewatkan angka lewat `float`.
