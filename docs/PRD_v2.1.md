# Payment Guard — PRD v2.1 (delta atas v2)

> Dokumen ini menggantikan bagian yang disebutkan di bawah. Bagian yang tidak disebut tetap berlaku dari PRD v2.
> Pendamping: Arsitektur v1.1, Security v1.1. Status: testnet only, reference implementation independen.
> Penulis: Chief of Strategy, 30 Okt 2026.

---

## 0. Changelog v2 → v2.1

| # | Topik | Keputusan |
|---|---|---|
| 1 | Trust boundary | Approvals CLI = sisi operator (bukan agent). Approve interaktif, tanpa `--yes`. (D1) |
| 2 | Surface tool | Guard hanya mengekspos `check_payment` dan `get_decision_status`. `execute_payment` = pembungkus lokal di host agent. (D2) |
| 3 | Mandate aktif | Disimpan di tabel `active_mandates` (DB), bukan hash file di disk. (D3) |
| 4 | Kedaluwarsa | `expires_at` di `decisions`; deteksi lazy; `expired` tidak memicu cooldown. |
| 5 | Mandate berganti | Approve atas decision lama → `denied (mandate_changed)`, tanpa cooldown. Cek 4b di Executor untuk execution baru. |
| 6 | Kode penolakan cek ulang | Cek ulang batas saat approve gagal → `denied (recheck_over_*)`, bukan `rejected_by_human`. |
| 7 | Urutan cek Executor | 1, 2, 3, 3b, 4, 5 (already_executed), 4b (mandate_not_active). |
| 8 | Recovery | Tidak memverifikasi ulang envelope atau mandate. |
| 9 | Serialisasi 4b | Advisory lock (shared di Executor, eksklusif di Guard start). Bukan `FOR SHARE`. |
| 10 | Lock sesi Guard | Koneksi dedikasi langsung ke Postgres + heartbeat. Putus = Guard berhenti (`guard_lock_lost`). |
| 11 | Role database | Wajib M0. Role grup NOLOGIN + role LOGIN per proses + matriks hak minimum. |
| 12 | Migrasi | SQL murni (psycopg 3), tanpa Alembic/SQLAlchemy. Runner kecil, forward-only. |
| 13 | Reason & event | Enum di kode + CHECK database untuk reason_code. Event di `events.py` + `docs/events.md`. |
| 14 | Tampilan approval | `reason` di blok kutipan tidak tepercaya, sanitasi daftar-izin, token konfirmasi anti-refleks. |
| 15 | Estimasi | M0 +0,5 hari. Total 10–12 hari tetap (hari 12 buffer). |
| 16 | Nama field snapshot | `latest_triggering_denial` menggantikan `latest_denial_at` (PRD v2 §14) karena penyaringan pemicu cooldown dilakukan pemanggil. |

---

## 3. Prinsip keamanan (revisi bagian 3)

1. **Testnet only** (Base Sepolia, chain id 84532). Wallet burner khusus tes.
2. Secret hanya di `.env` / file kunci (gitignored). Tidak pernah dicetak ke log.
3. Agent tidak pernah memegang private key wallet atau key tanda tangan.
4. **Pemisahan kunci per proses dan konfigurasi.** Key tanda tangan Guard dibaca Guard dan Approvals CLI. Key wallet dibaca Executor. Executor hanya punya public key Guard. Ini bukan pemisahan user OS (lihat Known limitations).
5. **Trust boundary operator:** Approvals CLI berjalan di sisi operator. Agent dikurung pada tiga tool, tanpa shell, tanpa akses file, tanpa browsing (D4).
6. Identitas agent ditentukan konfigurasi server (mandate), bukan argumen tool.
7. Executor membangun transaksi hanya dari field payload yang sudah diverifikasi.
8. Semua keputusan dan kejadian penting masuk `audit_log` (append-only, dijaga trigger dan hak akses).
9. `gitleaks` sebagai pre-commit; `git grep` pola key sebelum rilis.
10. Field `reason` dari agent: maks 500 karakter, data saja, tidak pernah mempengaruhi keputusan, tidak pernah masuk prompt LLM, ditampilkan dengan sanitasi daftar-izin dalam blok tidak tepercaya.

---

## 4. Komponen (revisi bagian 4)

| Komponen | Tugas | Tambahan v2.1 |
|---|---|---|
| **Guard** | MCP server (stdio), satu per agent. Tool: `check_payment`, `get_decision_status` saja. | Memegang session lock `guard:<agent_id>` di koneksi dedikasi. Upsert `active_mandates` saat start. |
| **Executor** | CLI `guard execute` (subproses) dan `guard executor recover`. | Cek 1–5, 4b. Advisory lock shared `mandate:<agent_id>` saat write-ahead. |
| **Pembungkus `execute_payment`** | Fungsi lokal di host agent. Memanggil `guard execute`, mengirim envelope lewat stdin. | Environment host tidak boleh berisi key atau URL RPC. Pembungkus membangun env subproses secara eksplisit. |
| **Approvals CLI** | `guard approvals list / approve / reject`. Interaktif saja. | Probe Guard hidup. Tampilkan drift mandate. Tampilkan `reason` sebagai blok tidak tepercaya. Konfirmasi token 6 karakter. |
| **Watcher** | Proses panjang, advisory lock per chain. | Filter `from` = wallet Executor. |
| **Reconciler** | CLI on-demand. | — |
| **Eval runner** | Adapter model netral, tiga kategori hasil (pass / fail / invalid). | Hanya tiga tool yang sama dengan agent demo. Stub `execute_payment`. |

---

## 6. Mandate & aturan (revisi bagian 6)

### 6.1 Mandate
Format sama dengan v2. Validasi saat load:
- `agent_id` cocok regex `^[a-z0-9][a-z0-9_-]{0,63}$`. Di luar itu ditolak (mencegah ambiguitas namespace kunci).
- Angka uang lewat parser (6.2), alamat EIP-55 bila campuran, TTL integer > 0.

`mandate_hash` = SHA-256 dari JSON kanonik ternormalisasi, dengan awalan `payment-guard/mandate/v1\n`. Disimpan di tabel `mandates` (append-only). Perubahan makna = hash baru.

### 6.2 Uang
Tidak berubah dari v2.

### 6.3 Jatah terpakai
Tidak berubah dari v2.

### 6.4 Urutan evaluasi `check_payment` (revisi)
Di dalam transaksi dengan advisory lock `agent:<agent_id>`:
0. Validasi input. Gagal → error input (tidak disimpan di `decisions`).
1. Cooldown: penolakan (`denied` selain `retry_blocked` dan `mandate_changed`, atau `rejected_by_human`) untuk agent + recipient, dalam `retry_cooldown_seconds` → `denied (retry_blocked)`.
2. Recipient tidak di allowlist → `denied (recipient_not_allowed)`.
3. `amount > max_per_tx` → `denied (over_per_tx_limit)`.
4. `committed_day + amount > max_per_day` → `denied (over_daily_limit)`.
5. `committed_recipient + amount > human_approval_above` → `needs_human (human_approval_required)`.
6. Selain itu → `approved (within_limits)`.

Batas inklusif pada sisi boleh. Kedaluwarsa `expires_at` untuk `needs_human` diisi `created_at + human_wait_ttl_seconds`.

**Cooldown tidak dipicu oleh:** `retry_blocked`, `mandate_changed`, `expired`. **Dipicu oleh:** `denied` lainnya termasuk `recheck_*`, dan `rejected_by_human`.

### 6.5 Alur `needs_human` (revisi)
- `needs_human` tidak memesan jatah.
- Approve, di dalam lock `agent:<agent_id>`:
  1. Jika `now >= expires_at`: ubah ke `expired (human_wait_timeout)`, `decided_at = expires_at`, audit `decision_expired`, tolak aksi.
  2. Jika `decision.mandate_hash` ≠ `active_mandates.mandate_hash`: `denied (mandate_changed)`.
  3. Cek ulang `per_tx` dan `daily` dengan data terbaru. Gagal → `denied (recheck_over_per_tx_limit | recheck_over_daily_limit)`.
  4. Lolos → `approved (approved_by_human)`, `approved_at = now`, `valid_until = now + decision_ttl_seconds`, tanda tangan, audit `approval_approved` dengan `guard_running`.
- Reject eksplisit → `rejected_by_human (human_rejected)`.
- Probe Guard: `pg_try_advisory_lock` pada `guard:<agent_id>`. Bila bebas (Guard mati), tampilkan peringatan dan minta konfirmasi tambahan. Lock segera dilepas.
- Layar konfirmasi menampilkan drift: "Approve memakai mandate AKTIF (hash X). File di disk (hash Y) baru berlaku setelah restart."

### 6.6 Mandate aktif (baru)
- Tabel `active_mandates` (satu baris per agent) diperbarui saat Guard start.
- `active_mandates` bukan kosong saat Guard mati. Mandate terakhir yang dimuat tetap berlaku sampai Guard start berikutnya.
- Edit file mandate tanpa restart tidak mengubah apa pun.

---

## 7. Kontrak tool (revisi)

Guard hanya mengekspos:

```
check_payment(recipient, amount, chain, reason) -> {
  decision_id, status, reason_code, agent_id, recipient, amount, amount_base,
  retry_after, mandate_hash, expires_at
}
get_decision_status(decision_id) -> bentuk respons sama, plus signed_decision bila approved
```

- `signed_decision` bisa muncul di `check_payment` (approved langsung) atau di `get_decision_status` (setelah approve manusia).
- `agent_id` tidak ada di skema tool.
- `execute_payment` bukan tool Guard. Pembungkus lokal di host.

### Envelope (revisi)
Payload menambahkan `mandate_hash` (64 hex lowercase). Aturan kanonik dan awalan domain `payment-guard/v1\n` tidak berubah.

---

## 9. Executor (revisi)

**Daftar cek** (dijalankan hanya untuk execution BARU, urutan tetap, gagal = berhenti + audit `execution_rejected`):

| # | Cek | Alasan penolakan (tertutup) |
|---|---|---|
| 1 | Signature valid untuk `key_id` yang dikenal dan tidak dicabut | `signature_invalid` |
| 2 | `v` dan `chain_id` cocok; `status == approved` | `version_or_chain_mismatch`, `not_approved` |
| 3 | Parse ketat: kunci ganda, field tak dikenal, tipe salah ditolak | `signature_invalid` |
| 3b | `now` (jam DB) ≤ `valid_until` | `expired` |
| 4 | Decision di DB identik dengan payload (termasuk `mandate_hash`) | `decision_mismatch` |
| 5 | Belum ada execution untuk decision ini (`UNIQUE(decision_id)`) | `already_executed` |
| 4b | `mandate_hash` payload = `active_mandates.mandate_hash` untuk `agent_id` (baris tidak ada = gagal) | `mandate_not_active` |
| 6 | Transaksi dibangun dari field payload | — |

Cek 4b dan write-ahead berada dalam satu transaksi, dengan `pg_advisory_xact_lock_shared(mandate:<agent_id>)` diambil sebelum membaca `active_mandates`. Urutan lock dalam transaksi Executor: `mandate:` dulu, lalu INSERT `executions`.

**Recovery** (`guard executor recover` dan awal setiap `guard execute`): bekerja pada baris `executions` yang sudah ada. Tidak memverifikasi ulang envelope, mandate, atau `valid_until`. Execution yang sudah `signed`/`broadcast` sudah dikomit: rebroadcast `raw_tx` yang sama, atau cek receipt. `failed` hanya bila nonce dipakai transaksi lain atau receipt revert.

---

## 10. Watcher & reorg
Tidak berubah dari v2, kecuali: Watcher memegang advisory lock `watcher:<chain_id>`, dan kedua transisi `final → reorged` tetap memakai alarm.

---

## 11. Rekonsiliasi
Tidak berubah dari v2, kecuali: `expired` untuk decision `approved` dihitung sejak `valid_until` lewat tanpa execution, bukan sejak `approved_at`.

---

## 12. Eval
Tidak berubah dari v2 untuk E1–E4, dengan tambahan:
- Dua model: Qwen3 1.7B (Ollama) dan satu model API. Hasil dilaporkan berdampingan.
- Kategori hasil per run: **pass / fail / invalid** (invalid = tidak ada tool call atau format rusak). Hitungan mentah, bukan hanya persen.
- Budget keras 5 USD. Runner berhenti bila estimasi biaya terlewati. `--dry-run` dan `--max-runs` tersedia.
- Catat nama+versi model, tanggal, temperature, jumlah run. Transkrip mentah di `evals/results/`.
- Data ke API hanya sintetis. Key API hanya dibaca eval runner.
- Adapter model netral: antarmuka sama untuk Ollama dan API.
- Harga dan ID model dicek ulang di dokumentasi resmi sebelum M8, bukan dari ingatan.

---

## 13. Tes wajib (ringkas, detail di Security v1.1)
Tes unit dan integrasi dari v2, ditambah:
- Kunci advisory: known-answer, namespace berbeda → kunci berbeda, rentang int64.
- Bootstrap role: idempoten, migrator tanpa `CREATEROLE` ditolak.
- Migrasi: database kosong, dijalankan dua kali, checksum berubah ditolak, celah/nomor ganda ditolak, rollback penuh bila gagal di tengah, multi-statement dengan `$$` jalan utuh.
- Hak akses: matriks dicek dua arah dengan `pg_class`, dan hak efektif dengan `has_*_privilege`.
- Cek 4b dan urutannya; recovery tetap jalan setelah mandate berganti dan setelah `valid_until` lewat.
- Lock sesi: heartbeat, matikan koneksi → Guard berhenti.

---

## 14. Milestone (revisi)

| Hari | Milestone | Tambahan v2.1 |
|---|---|---|
| 1 | M0 Scaffold | Runner migrasi, `0002_roles.sql`, `bootstrap-roles`, tes hak akses, notice handler, pin Python dan Postgres 16. |
| 2 | M1 Uang & aturan murni | — |
| 3 | M2 Signing | Parser envelope ketat. |
| 4 | M3 Decision service + MCP | Lock sesi di koneksi dedikasi, heartbeat, `active_mandates`. |
| 5 | M4 Human approval | Probe, drift warning, layar K2. |
| 6 | M5 Executor | Cek 1–5, 4b, lock shared, recovery. Faucet dan RPC sudah siap. |
| 7 | M6 Watcher | Verifikasi USDC tiga sumber, `decimals()`. |
| 8 | M7 Reorg + Reconciler | — |
| 9–10 | M8 Eval | Dua model, adapter, budget. |
| 11 | M9 Polish | README, `docs/events.md` sinkron, CHECK event dipertimbangkan. |
| 12 | Buffer | — |

---

## 16. Known limitations (revisi)
- Pemisahan kunci per proses dan konfigurasi, bukan per user OS. Penyerang dengan akses user yang sama bisa membaca semua.
- Agent yang tidak dikurung (shell, file) berada di luar model ancaman v1.
- Operator yang sah bisa mengganti mandate (terlihat di audit, tidak dicegah).
- Execution yang sudah `signed`/`broadcast` tidak dibatalkan saat mandate berganti.
- Approval tidak terikat ke identitas operator (tidak ada autentikasi).
- Trigger dan hak akses tidak melindungi dari pemilik objek atau superuser. Pemilik objek = role migrator.
- Role yang dibobol bisa menahan advisory lock (dibatasi `lock_timeout`).
- Satu RPC, tanpa failover. Finality = N konfirmasi (soft finality L2).
- Token konfirmasi anti-refleks, bukan kontrol keamanan terhadap agent.
- Bukan audit, bukan production.

---

## 17. Future work
Signer terpisah (opsi b), autentikasi operator, EIP-712 on-chain, rotasi kunci otomatis, role DB per proses lebih ketat, hash-chain audit, rate limit per agent, provider failover, multi-chain, tag blok `finalized`, dashboard read-only, notifikasi Telegram.
