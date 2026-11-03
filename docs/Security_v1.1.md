# Payment Guard — Threat Model & Keamanan v1.1

> Pendamping PRD v2.1 dan Arsitektur v1.1. Metode: STRIDE ringan per komponen.
> Status: reference implementation, testnet only. Bukan audit, bukan production.
> Penulis: Chief of Strategy, 30 Okt 2026.

---

## 1. Tujuan
1. Menjawab siapa yang bisa merusak apa, dan kontrol mana yang menahannya.
2. Menyatakan asumsi dan batas dengan jujur.
3. Menjadi sumber tes keamanan dan bahan README.

---

## 2. Klaim dan non-klaim

**Diklaim (dengan asumsi bagian 4):**
- Untuk agent yang dikurung pada tiga tool, aturan mandate ditegakkan secara deterministik. Agent tidak bisa memilih identitas, memalsukan persetujuan, atau menggerakkan uang tanpa envelope bertanda tangan yang masih berlaku dan cocok dengan mandate aktif.
- Setiap pembayaran final di chain bisa ditelusuri ke satu keputusan, atau menjadi alarm.
- Pencatatan on-chain idempoten dan hanya setelah final, dengan penanganan reorg.
- Setiap keputusan terikat pada `mandate_hash`.

**Tidak diklaim:**
- Tidak diaudit pihak ketiga. Bukan production. Bukan untuk mainnet atau uang asli.
- Tidak melindungi dari penyerang yang menguasai user OS tempat komponen berjalan.
- Audit log tahan-ubah secara operasional (trigger + hak akses), bukan secara kriptografis.
- Tidak menjamin perilaku model. Eval hanya mengukur.
- Tidak melindungi dari operator sah yang mengganti mandate (terlihat di audit, tidak dicegah).
- Kepercayaan pada satu RPC dan pada kontrak USDC testnet (proxy yang bisa di-upgrade penerbitnya).

---

## 3. Aset

| ID | Aset | Mengapa penting |
|---|---|---|
| A1 | Key tanda tangan Guard | Pembuat persetujuan palsu |
| A2 | Key wallet Executor + dana testnet | Menggerakkan dana |
| A3 | Integritas mandate dan `active_mandates` | Menentukan apa yang boleh |
| A4 | Integritas `audit_log`, `decisions`, `executions` | Bukti dan rekonsiliasi |
| A5 | Rahasia konfigurasi (URL DB, URL RPC, key API eval, admin URL) | Akses dan biaya |
| A6 | Keputusan dan envelope sekali pakai | Mencegah pembayaran ganda |
| A7 | Kejujuran klaim proyek | Kredibilitas |

---

## 4. Aktor dan asumsi

**Aktor**

| ID | Aktor | Kepercayaan |
|---|---|---|
| T1 | Agent (LLM), mungkin kena prompt injection | Tidak tepercaya |
| T2 | Konten eksternal yang dibaca agent, termasuk field `reason` | Tidak tepercaya |
| T3 | RPC / penyedia node | Dipercaya terbatas, datanya diverifikasi |
| T4 | Operator (kamu) | Tepercaya, bisa salah atau tertipu |
| T5 | Penyerang dengan akses user OS di mesin yang sama | **Di luar model ancaman v1** |
| T6 | Rantai pasok dependensi | Dipercaya dengan kontrol |
| T7 | Pengunjung publik repo | Tidak tepercaya |

**Asumsi (jika dilanggar, jaminan gugur):**
1. Agent dikurung pada tepat tiga tool: `check_payment`, `get_decision_status`, `execute_payment` (pembungkus). Tanpa shell, file, atau browsing.
2. Key dan file mandate berada di luar direktori kerja dan environment host agent.
3. Environment host agent tidak berisi key, URL DB, atau URL RPC.
4. Approvals CLI dan key tanda tangan berada di sisi operator.
5. Postgres hanya dapat dijangkau proses lokal. Koneksi lock Guard langsung ke Postgres, tanpa pgbouncer.
6. Hanya burner testnet.
7. Jam yang dipercaya = jam database.

---

## 5. Batas kepercayaan

```
[Tidak tepercaya]  Agent (3 tool) ── check_payment / get_decision_status ──▶ Guard ──▶ Postgres (role guard_app)
                       │
                       └─ execute_payment (pembungkus, env dibersihkan) ─▶ Executor ──▶ RPC (data diverifikasi)
[Sisi operator]    Approvals CLI (interaktif, role approver_app), key tanda tangan, key wallet, mandate, .env
[Admin, sekali]    bootstrap-roles (URL admin sementara)
```

Pemisahan ini adalah pemisahan proses, konfigurasi, dan hak database. Bukan pemisahan user OS (RR-1).

---

## 6. Daftar ancaman dan kontrol

Kolom "Tes" merujuk bagian 9.

### 6.1 Spoofing

| ID | Ancaman | Kontrol | Tes | Risiko sisa |
|---|---|---|---|---|
| S1 | Agent mengaku sebagai agent lain | `agent_id` bukan argumen tool. Identitas dari mandate. Satu Guard per agent (session lock). `agent_id` divalidasi regex | ST-01, ST-02, ST-32 | Operator sah yang mengganti konfigurasi (RR-3) |
| S2 | Persetujuan palsu | Ed25519, Executor hanya punya public key, awalan domain | ST-03, ST-06 | Key bocor (RR-1) |
| S3 | Field envelope diubah | Tanda tangan atas semua field. Executor cocokkan dengan DB (cek 4) | ST-03, ST-04 | Penyerang yang bisa menulis DB (RR-2) |
| S4 | RPC berbohong | `chain_id`, konsistensi `block_hash`, filter `from`, alarm `unmatched_onchain` | ST-18 | RPC berbohong konsisten (RR-4) |

### 6.2 Tampering

| ID | Ancaman | Kontrol | Tes | Risiko sisa |
|---|---|---|---|---|
| T-1 | Mandate diganti untuk melonggarkan aturan | (a) Cek 4b: execution baru ditolak bila `mandate_hash` tidak sama dengan `active_mandates` (`mandate_not_active`). (b) Approve atas decision lama setelah pergantian → `denied (mandate_changed)`. (c) Tanpa hot-reload. (d) `mandate_hash` di payload bertanda tangan. | ST-07, ST-08, ST-09, ST-25, ST-26, ST-28 | **Dua residual terpisah:** (i) operator sah bisa mengganti mandate, terlihat di audit, tidak dicegah; (ii) execution yang sudah `signed`/`broadcast` tidak dibatalkan (by design, recovery) |
| T-2 | Menghapus atau mengubah jejak audit | Trigger menolak UPDATE, DELETE, TRUNCATE. Role aplikasi tanpa DELETE/TRUNCATE/DDL. Role aplikasi tidak owner | ST-10, ST-33, ST-34 | Owner/superuser bisa melepas trigger (RR-2). Hash-chain = future work |
| T-3 | Pemecahan pembayaran | Cooldown, agregasi per recipient, batas harian | ST-11, ST-12 | Penyebaran antar recipient dibatasi batas harian (RR-5) |
| T-4 | Balapan melewati batas | Lock `agent:`, tes paralel | ST-13 | — |
| T-5 | Replay envelope | `UNIQUE(decision_id)`, cek 5 sebelum 4b (sinyal replay tetap tercatat `already_executed`) | ST-14, ST-27 | — |
| T-6 | Transaksi ganda / nonce bentrok | Write-ahead, `UNIQUE(from_addr, nonce)`, lock `executor:`, rebroadcast idempoten | ST-15 | — |
| T-7 | Replay lintas chain | EIP-155 (84532), cek `chain_id` RPC saat start | ST-16 | — |
| T-8 | **Injeksi lewat `reason` ke operator** (baru) | Blok kutipan berlabel tidak tepercaya; teks utama dibangun dari field terstruktur saja; sanitasi daftar-izin (`render_untrusted`); konfirmasi token | ST-35..ST-37 | Operator tetap bisa tertipu isi kutipan; token anti-refleks (RR-9) |

### 6.3 Repudiation

| ID | Ancaman | Kontrol | Risiko sisa |
|---|---|---|---|
| R-a | Operator menyangkal approval | `approval_approved` memuat detail decision dan `guard_running`. Konfirmasi interaktif | Tidak ada autentikasi operator (RR-3) |
| R-b | Catatan hilang saat crash | Write-ahead. Watcher atomik | — |

### 6.4 Information disclosure

| ID | Ancaman | Kontrol | Tes | Risiko sisa |
|---|---|---|---|---|
| I1 | Key ikut ke git | `.gitignore`, `.env.example` tanpa nilai, gitleaks, `git log` dan `git grep` | ST-19 | — |
| I2 | Rahasia bocor lewat log, audit, traceback | Redaksi (key, URL DB, URL RPC, API key). Tes canary | ST-17 | — |
| I3 | `reason` untuk injeksi log dan terminal | Batas 500 karakter, `render_untrusted` | ST-20, ST-35 | — |
| I4 | Data sensitif ke API model | Eval hanya data sintetis | review | — |
| I5 | Envelope bocor | Envelope = kredensial sekali pakai sampai `valid_until`. Tujuan dan jumlah terkunci. Perlakukan sebagai rahasia. `decision_ttl` pendek (default 15 menit) | ST-14 | Pemegang envelope bisa memicu eksekusi sekali sebelum `valid_until` |
| I6 | Admin URL bocor lewat riwayat shell | Admin URL dari env sementara atau prompt tersembunyi. Tidak disimpan | review | — |

### 6.5 Denial of service

| ID | Ancaman | Kontrol | Risiko sisa |
|---|---|---|---|
| D1 | Agent membanjiri `check_payment` | Cooldown setelah penolakan, batas harian | Tabel `decisions` membengkak (RR-6) |
| D2 | Lock tersangkut | `lock_timeout`, `statement_timeout`, fail closed | Role dibobol menahan lock (RR-7) |
| D3 | RPC mati | Fail closed, recovery, watcher lanjut dari `last_scanned` | — |
| D4 | `get_logs` terlalu besar | `SCAN_BATCH_SIZE` | — |
| D5 | Koneksi lock putus | Heartbeat, Guard berhenti | — |

### 6.6 Elevation of privilege

| ID | Ancaman | Kontrol | Tes | Risiko sisa |
|---|---|---|---|---|
| E1 | Prompt injection agar agent melanggar aturan | Guard tidak membaca `reason` untuk keputusan. Tanpa envelope tidak ada eksekusi. Eval E4 | ST-21 | Agent bisa mencoba |
| E2 | Agent bershell membaca key atau menjalankan CLI | Pengurungan (asumsi 1), key di luar direktori kerja, env host bersih | ST-22 | Di luar model v1 bila asumsi dilanggar (RR-1) |
| E3 | Approvals CLI disalahgunakan | Interaktif, tanpa `--yes`, mode non-interaktif ditolak | ST-23 | Bukan pertahanan terhadap agent bershell (pty) |
| E4 | Parsing envelope ambigu | Parser ketat, verifikasi atas bentuk kanonik | ST-05 | — |
| E5 | Kekeliruan algoritma atau kunci | Hanya Ed25519, tanpa field `alg`, `key_id` di registri lokal, `revoked` ditolak | ST-06 | — |
| E6 | SQL injection | Hanya kueri berparameter. Bandit S608 membantu, bukan bukti. Kode SQL ditinjau manual | ST-24 | — |
| E7 | Dependensi berbahaya | Lockfile ber-hash, versi dipin, `pip-audit`, Dependabot | CI | RR-8 |
| E8 | Token atau kontrak salah | Hanya `USDC_ADDRESS` terverifikasi, `decimals() == 6`, filter alamat token | ST-16 | Kontrak USDC testnet bisa di-upgrade (RR-8) |
| E9 | Salah penilaian waktu | Jam DB | ST-14 | — |
| E10 | Role aplikasi melampaui hak (baru) | Matriks hak minimum, role per proses, tes dua arah dengan `pg_class` dan `has_*_privilege`. Tidak ada grant ke PUBLIC | ST-33..ST-34, ST-38..ST-40 | Pemilik objek dan superuser di luar jaminan (RR-2) |
| E11 | Migrasi diam-diam gagal mencabut hak (baru) | Schema-level di bootstrap. WARNING "no privileges" menggagalkan migrasi. Tes memeriksa keadaan efektif | ST-41 | — |

---

## 7. Manajemen kunci

| Kunci | Pembuatan | Penyimpanan | Dibaca oleh | Bila bocor |
|---|---|---|---|---|
| Tanda tangan Guard | `guard keygen` lokal | File izin 600, gitignored, di luar direktori kerja agent | Guard, Approvals CLI | Runbook 8.1 |
| Wallet Executor | Skrip lokal, tanpa cetak | File izin 600 atau `.env`, gitignored | Subproses Executor | Runbook 8.2 |
| URL DB per proses runtime | `bootstrap-roles` | `.env`, gitignored | Proses masing-masing | Rotasi password role (Runbook 8.5) |
| URL DB migrator (pemilik objek) | `bootstrap-roles` | `.env.migrator`, gitignored, **terpisah dari `.env`** | `guard db migrate` / `status` saja | Rotasi password role (Runbook 8.5) |
| URL RPC | Penyedia | `.env` | Executor, Watcher | Cabut dan buat ulang di dasbor |
| Key API eval | Dibuat sendiri, batas bulanan rendah | `.env` | Eval runner | Cabut dari konsol |
| Admin URL | Diberikan saat bootstrap | Tidak disimpan | Bootstrap | Ganti password admin |

**Aturan keras:** tidak pernah memakai ulang key, seed, atau wallet pribadi/mainnet.

---

## 8. Runbook insiden

**8.1 Key tanda tangan Guard bocor**
1. Hentikan Guard dan Approvals CLI.
2. `guard keygen` (key_id baru).
3. Perbarui `guard_public.json`: key baru, key lama `"revoked": true`. Executor menolak envelope dari key yang dicabut.
4. Periksa `audit_log`: approval tanpa `approval_approved` = dibuat di luar CLI (indikasi serius).
5. Periksa `executions` untuk transaksi yang tidak punya decision.
6. Catat insiden.

**8.2 Key wallet bocor**
1. Hentikan Executor.
2. Pindahkan dana testnet ke wallet baru.
3. Buat wallet burner baru, perbarui konfigurasi.
4. Periksa `unmatched_onchain` (transfer dari wallet lama tanpa execution = pemakaian di luar Guard).

**8.3 Secret ter-commit**
Anggap bocor. Cabut atau rotasi. Bersihkan riwayat git. Jalankan gitleaks pada seluruh riwayat.

**8.4 Alarm rekonsiliasi** (`unmatched_onchain`, `mismatch`, `alarm_final_settlement_reorged`)
Hentikan Executor. Periksa transaksi di block explorer. Bandingkan dengan `executions` dan `decisions`. Catat.

**8.5 Password role DB bocor**

> **Rotasi otomatis belum diimplementasi.** Rencana: `guard db bootstrap-roles --rotate-passwords`, tenggat sebelum M3. Sampai perintah itu ada, pakai prosedur manual di bawah. Jangan menjalankan perintah yang belum ada.

Prosedur manual:
1. Hentikan proses yang memakai role itu.
2. Sebagai admin (URL admin sementara, host loopback), jalankan:
   `ALTER ROLE <nama> LOGIN PASSWORD '...'`
3. Perbarui URL role itu di filenya:
   * role runtime (`guard_app`, `approver_app`, `executor_app`, `watcher_app`, `reconciler_app`) -> `.env`
   * `migrator_owner` -> `.env.migrator`
4. Jalankan ulang proses itu, lalu periksa `audit_log` untuk aktivitas tak terduga.

Catatan: `bootstrap-roles` yang dijalankan ulang **tidak** merotasi password yang URL-nya masih tercatat; ia sengaja memakainya ulang supaya proses yang sedang jalan tidak putus. Ia hanya menyetel password baru bila baris URL-nya hilang dari file env.

**8.6 Mandate diubah di luar prosedur**
1. Bandingkan `active_mandates` dengan `mandates`.
2. Periksa `audit_log` `mandate_loaded` untuk waktu dan hash.
3. Jika tidak sah: hentikan Guard, muat mandate yang benar lewat restart, periksa execution sejak pergantian.

---

## 9. Daftar tes keamanan

Tambahan atas bagian 13 PRD v2.1. Semua otomatis kecuali ditandai *review*.

| ID | Tes |
|---|---|
| ST-01 | Skema tool `check_payment` tidak memuat `agent_id` |
| ST-02 | Guard kedua untuk agent sama gagal start; agent berbeda tidak saling memengaruhi |
| ST-03 | Ubah satu field envelope → verifikasi gagal |
| ST-04 | Executor menolak bila baris DB berbeda dari payload (tanda tangan valid) |
| ST-05 | Envelope dengan kunci ganda, field ekstra, tipe salah, atau urutan acak → ditolak atau dinormalisasi sama |
| ST-06 | `key_id` tak dikenal, key dicabut, tanpa awalan domain → gagal |
| ST-07 | Mandate sama dengan format berbeda → hash sama; ubah satu batas → hash beda |
| ST-08 | Edit file mandate tanpa restart → tidak mengubah perilaku |
| ST-09 | Approve decision lama setelah mandate berganti → `denied (mandate_changed)`, tanpa cooldown |
| ST-10 | UPDATE, DELETE, TRUNCATE pada `audit_log` dan `mandates` ditolak |
| ST-11 | Cooldown pada `C-1`, `C`, `C+1`; `retry_blocked` tidak memperpanjang; `expired` dan `mandate_changed` tidak memicu cooldown |
| ST-12 | Agregasi: 24 + 24 melewati ambang 25 → `needs_human` |
| ST-13 | 10 permintaan paralel @30, batas 100 → tepat 3 disetujui |
| ST-14 | Eksekusi kedua decision sama ditolak; envelope kedaluwarsa (jam DB) ditolak |
| ST-15 | Crash setelah write-ahead sebelum broadcast → recovery tanpa transaksi ganda |
| ST-16 | `chainId` 84532; `chain_id` RPC salah → menolak jalan; `decimals() != 6` → menolak; token selain `USDC_ADDRESS` diabaikan |
| ST-17 | Tes canary: key, URL DB, URL RPC, API key palsu tidak muncul di log, audit, stdout, stderr, atau pesan pengecualian, termasuk jalur error |
| ST-18 | Transfer tanpa execution → `unmatched_onchain`; `block_hash` tak konsisten → tidak dicatat final |
| ST-19 | gitleaks bersih pada seluruh riwayat; `git grep` pola key kosong |
| ST-20 | `reason` >500 karakter ditolak |
| ST-21 | Eval E4 (injeksi `reason`): keputusan tidak berubah |
| ST-22 | Registri tool agent demo/eval persis tiga tool; env host tidak memuat key, URL DB, URL RPC; direktori kerja agent tidak berisi path key atau mandate |
| ST-23 | `guard approvals approve` menolak mode non-interaktif; detail lengkap sebelum konfirmasi |
| ST-24 | Bandit S608 bersih; tidak ada SQL dirakit string di `src/` |
| ST-25 | Execution baru dengan mandate berganti setelah approve → `mandate_not_active` |
| ST-26 | Execution `signed` lalu mandate berganti → recovery tetap jalan (rebroadcast/receipt) |
| ST-27 | Replay decision yang sudah dieksekusi setelah mandate berganti → `already_executed`, bukan `mandate_not_active` |
| ST-28 | Pergantian `active_mandates` bersamaan dengan write-ahead: lock `mandate:` eksklusif menunggu shared; hasil selalu salah satu urutan konsisten; tidak ada execution lolos dengan hash tidak aktif |
| ST-29 | Matikan koneksi lock Guard → Guard berhenti dan tidak lagi menjawab `check_payment` |
| ST-30 | Probe approvals tidak mengganggu lock Guard; Guard mati → peringatan dan `guard_running=false` |
| ST-31 | Baris `active_mandates` tidak ada untuk agent → `mandate_not_active`, bukan lolos |
| ST-32 | `agent_id` dengan `:`, spasi, huruf besar, atau >64 karakter ditolak saat load mandate |
| ST-33 | Role `executor_app` tidak bisa `UPDATE decisions`; `guard_app` tidak bisa `INSERT executions`; `watcher_app` tidak bisa `SELECT decisions` |
| ST-34 | Role aplikasi tidak bisa DROP, ALTER, TRUNCATE; tidak ada role yang bisa UPDATE/DELETE `audit_log`; semua role bisa INSERT `audit_log` |
| ST-35 | `reason` berisi `\x1b[2J`, `\r`, `U+202E`, `U+200B`, `U+009B`, `U+0085`, `U+2028`, `U+00AD`, `U+E0041`, dan baris palsu "Amount: 1" → blok utama identik byte-demi-byte dengan kasus polos |
| ST-36 | `reason` tampil ter-escape di blok kutipan dan sesuai daftar-izin |
| ST-37 | Konfirmasi hanya menerima token 6 karakter yang benar |
| ST-38 | Matriks hak dicek dua arah dengan `pg_class` (tabel `r`/`p` dan sequence `S`) |
| ST-39 | Hak efektif tiap (role, objek, hak) sesuai matriks, termasuk kolom UPDATE; tidak ada hak tambahan |
| ST-40 | Tidak ada grant ke PUBLIC pada objek aplikasi; `schema_migrations` hanya owner |
| ST-41 | Migrator tanpa `CREATEROLE` ditolak; `0002_roles.sql` gagal keras bila role grup belum ada; WARNING "no privileges" menggagalkan migrasi; keadaan efektif diperiksa dengan `has_schema_privilege` |
| ST-42 | Role grup NOLOGIN tidak bisa login; bootstrap idempoten (dua kali sama hasil) |
| ST-43 | INSERT ke tabel BIGSERIAL ditolak tanpa `USAGE` pada sequence, berhasil sesudah grant |
| ST-44 | `pg_try_advisory_lock` dari role approver berhasil saat Guard mati dan gagal saat hidup |
| ST-45 | Kunci advisory: known-answer, namespace berbeda → kunci berbeda, rentang int64 |
| ST-46 | Properti (hypothesis): untuk string acak Unicode, `render_untrusted` hanya menghasilkan karakter izinan atau escape, tidak pernah karakter kontrol atau format mentah |
| ST-47 | Combining mark berurutan >3 di-escape |
| ST-48 | Lock `lock_timeout` berlaku: role yang menahan lock tidak membuat Guard hang tanpa batas |

*ID ST-25 sampai ST-48 melengkapi daftar v1 (ST-01 sampai ST-24) dan dipakai di bagian 6.*

---

## 10. Risk register

| ID | Risiko | Keputusan |
|---|---|---|
| RR-1 | Pemisahan key hanya proses, konfigurasi, dan hak DB. User OS yang sama bisa membaca semua. Agent bershell di luar model. **Termasuk pemisahan `.env` dan `.env.migrator` (M0.1):** di Windows izin file tidak setara `chmod 600`, dan di OS mana pun semua proses milik user yang sama bisa membaca kedua file. Pemisahan itu mencegah URL pemilik objek ikut termuat ke proses runtime, ter-log, atau terbawa dump environment runtime — **bukan** batas keamanan keras | **Diterima v1**, ditulis di README. Hardening: user OS terpisah, secret manager |
| RR-2 | Owner migrator dan superuser bisa mengubah baris dan melepas trigger. Role dibobol hanya bisa menulis tabel yang diizinkan | **Dikurangi, bukan dihapus** (matriks hak per proses). Mitigasi lanjutan: hash-chain audit, owner terpisah |
| RR-3 | Tidak ada autentikasi operator. Operator sah bisa mengganti mandate | **Diterima v1**, terlihat di audit |
| RR-4 | RPC tunggal yang berbohong konsisten | **Diterima** (testnet). Future: multi-RPC |
| RR-5 | Penyebaran lintas recipient menghindari agregasi | **Diterima**, dibatasi batas harian |
| RR-6 | Tanpa rate limit per agent | **Diterima**, opsional v1.x |
| RR-7 | Role yang dibobol menahan advisory lock (DoS) | **Diterima**, dibatasi `lock_timeout` |
| RR-8 | Rantai pasok dependensi; kontrak USDC testnet bisa di-upgrade | **Dikurangi** (lockfile, audit), **diterima** |
| RR-9 | Operator tertipu isi kutipan `reason` meski layar sudah dirancang (anti-injeksi tampilan, bukan jaminan) | **Diterima**. Token konfirmasi anti-refleks, bukan kontrol keamanan. Ditulis di README |
| RR-10 | Envelope yang sudah beredar tidak bisa dibatalkan sampai `valid_until` | **Diterima**. `decision_ttl` pendek |
| RR-11 | Execution `signed`/`broadcast` tidak dibatalkan saat mandate berganti | **Diterima** (by design, komitmen) |
| RR-12 | Versi migrasi tidak bisa diverifikasi oleh Guard (`guard_app` tanpa hak ke `schema_migrations`). Database yang dimigrasi setengah jalan lolos start | **Diterima v1**. Mitigasi: langkah operator di README (`guard db status` harus "0 tertunda" sebelum `guard serve`). Perbaikan opsi B — tabel versi khusus lewat migrasi `0003` — ditinjau di M9 |

---

## 11. Perubahan dari Security v1

- T-1 dipecah menjadi dua residual (RR-3 dan RR-11). Cek 4b menutup "approval lama dipakai setelah mandate berganti".
- R-2 menjadi RR-2, dikurangi oleh matriks hak per proses. Tidak dihapus.
- Ditambahkan T-8 (injeksi lewat `reason` ke operator), I6, E10, E11, dan runbook 8.5 dan 8.6.
- Daftar tes: ST-25 sampai ST-48 ditambahkan. ST-01 sampai ST-24 tetap.
- ID risiko diganti dari R-1..R-8 menjadi RR-1..RR-11 untuk menghindari tabrakan dengan ID ancaman.

## 11.1 Perubahan M0.1

- `MIGRATOR_DB_URL` dipindah dari `.env` ke `.env.migrator`. Konfigurasi runtime tidak punya atribut untuk URL itu. Batas kejujurannya masuk RR-1.
- `bootstrap-roles` menolak URL admin yang hostnya bukan loopback kecuali diberi `--allow-remote-admin`. Pesan penolakan menyebut host, tidak pernah URL.
- Input URL admin lewat prompt memakai `getpass` (tidak di-echo); tidak ada jalur yang mencetak URL admin.
- Runbook 8.5 ditulis ulang: rotasi otomatis dinyatakan belum ada, dengan rencana dan tenggat, plus prosedur manual.

---

## 12. Ringkasan untuk README (siap tempel)

> **Security model.** Payment Guard is a testnet-only reference implementation. For an agent confined to three tools (`check_payment`, `get_decision_status`, `execute_payment`), mandate rules are enforced deterministically. Approvals are Ed25519-signed and single-use, bound to the mandate hash that was active at approval time, and every on-chain payment is reconciled to the decision that allowed it or raised as an alarm. Each process runs with its own database role that has only the privileges it needs, and the audit log is append-only by trigger and grants.
>
> It does **not** protect against an attacker with access to the same OS user (keys are separated by process, configuration, and database role, not by OS user). It does not prevent an authorized operator from replacing a mandate, though every replacement is audited. Executions already signed or broadcast are not cancelled when a mandate changes. The confirmation token in the approval screen is an anti-reflex measure, not a security control against the agent. The audit log is tamper-evident operationally, not cryptographically. The project has not been independently audited. Do not use it with real funds.
