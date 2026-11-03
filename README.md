# Payment Guard

Reference implementation: penegakan aturan mandate yang deterministik untuk
pembayaran yang diminta agent. **Testnet only. Bukan audit, bukan production,
bukan untuk uang asli.**

Dokumen yang mengikat: [docs/PRD_v2.1.md](docs/PRD_v2.1.md),
[docs/Architecture_v1.1.md](docs/Architecture_v1.1.md),
[docs/Security_v1.1.md](docs/Security_v1.1.md). Sumber kebenaran skema:
[migrations/0001_init.sql](migrations/0001_init.sql) dan
[migrations/0002_roles.sql](migrations/0002_roles.sql).

Status: **M0 (scaffold)**. Yang sudah ada adalah struktur repo, runner migrasi,
role database, dan tesnya. Komponen runtime (Guard, Executor, Approvals CLI,
Watcher, Reconciler) menyusul di M1 dan sesudahnya. README lengkap adalah
pekerjaan M9.

## Prasyarat

- Python 3.12 (dipin di `.python-version`)
- [uv](https://docs.astral.sh/uv/)
- Docker, untuk Postgres 16 lokal

## Menyiapkan dari nol

```bash
# 1. Dependensi, dari lockfile ber-hash
uv sync

# 2. Postgres 16 untuk project ini. Container, jaringan, dan volume-nya
#    diberi nama `payment-guard*` supaya tidak tercampur dengan project lain
#    di mesin yang sama. Port host: 5434, terikat ke 127.0.0.1.
docker compose up -d

# 3. Role database. Dijalankan admin, sekali. URL admin diberikan lewat
#    environment variable SEMENTARA dan tidak pernah disimpan.
#    Tanpa variabel ini, perintah meminta URL lewat prompt tersembunyi.
PAYMENT_GUARD_ADMIN_DB_URL='postgresql://guard_admin:devonly_not_a_secret@127.0.0.1:5434/payment_guard' \
  uv run guard db bootstrap-roles

# 4. Skema. Dijalankan role migrator, yang URL-nya baru ditulis ke
#    .env.migrator oleh langkah 3.
uv run guard db migrate
uv run guard db status
```

Langkah 3 membuat role grup NOLOGIN (`guard_rw` … `reconciler_rw`), role LOGIN
per proses (`guard_app` … `reconciler_app`), dan role pemilik objek
(`migrator_owner`). Password dibuat acak dan **tidak pernah dicetak**.

URL-nya ditulis ke **dua file terpisah**:

| File | Isi | Dibaca oleh |
|---|---|---|
| `.env` | lima URL runtime | proses runtime |
| `.env.migrator` | `MIGRATOR_DB_URL` saja | `guard db migrate`, `guard db status` |

Keduanya di-gitignore. Contoh tanpa nilai ada di `.env.example` dan
`.env.migrator.example`.

Role migrator memiliki semua objek dan karena itu bisa melepas trigger
append-only. Memisahkan filenya mencegah URL-nya ikut termuat ke proses
runtime, ikut ter-log, atau terbawa dump environment runtime. Ini **bukan**
batas keamanan keras: di Windows izin file tidak setara `chmod 600`, dan di OS
mana pun semua proses milik user yang sama bisa membaca kedua file. Lihat RR-1
di [docs/Security_v1.1.md](docs/Security_v1.1.md).

`MIGRATOR_DB_URL` dari environment tetap diterima `migrate` dan `status`
sebagai override yang sengaja dibuka untuk CI, supaya CI bisa menyuntikkan URL
tanpa menulis rahasia ke disk; override itu tidak pernah menggantikan atau
memuat `.env` runtime, dan environment proses sendiri bisa dibaca proses mana
pun di user yang sama — batas yang sama dengan RR-1.

`bootstrap-roles` **menolak URL admin yang hostnya bukan loopback**, karena ia
membuat role dan mengubah hak tingkat schema. Untuk cluster jauh yang memang
disengaja, tambahkan `--allow-remote-admin`.

Dijalankan dua kali, langkah 3 tidak mengubah apa pun: URL yang sudah tercatat
dipakai ulang, bukan dirotasi.

## Menjalankan Guard

**Periksa dulu bahwa skemanya mutakhir:**

```bash
uv run guard db status      # harus menampilkan "0 tertunda"
uv run guard db migrate     # hanya bila ada yang tertunda
uv run guard serve
```

Langkah pertama bukan formalitas. Guard berjalan sebagai `guard_app`, yang
**tidak punya hak ke `schema_migrations`** dan karena itu tidak bisa memeriksa
versi skema sendiri. Saat start ia hanya memastikan tabel yang dibutuhkan ada,
jadi **database yang dimigrasi setengah jalan akan lolos start** dan baru gagal
nanti, di tengah keputusan. Memeriksanya di sini memindahkan kegagalan itu ke
tempat yang benar.

Kalau `status` menampilkan baris `tertunda`, jalankan `guard db migrate` lebih
dulu, lalu `status` sekali lagi untuk memastikan.

Batas ini diterima dengan sengaja: lihat RR-12 di
[docs/Security_v1.1.md](docs/Security_v1.1.md) dan bagian 3.2 di
[docs/Architecture_v1.1.md](docs/Architecture_v1.1.md).

`guard serve` membaca `.env` saja, tidak pernah `.env.migrator`.
`GUARD_SIGNING_KEY_PATH` **harus** menunjuk ke luar akar repo; Guard menolak
start kalau tidak. Lihat `.env.example`.

## Perintah yang tersedia di M0

| Perintah | Dijalankan sebagai | Membaca | Guna |
|---|---|---|---|
| `guard db bootstrap-roles` | admin, sekali | env sementara / prompt | Role, keanggotaan grup, hak tingkat schema. Menulis `.env` dan `.env.migrator` |
| `guard db migrate` | migrator | `.env.migrator` | Terapkan migrasi yang belum terapan |
| `guard db status` | migrator | `.env.migrator` | Laporkan migrasi terapan dan tertunda |

Flag yang ada: `--allow-remote-admin` dan `--no-prompt` (bootstrap),
`--env-file` (bootstrap), `--migrator-env-file` (ketiganya),
`--migrations-dir` (migrate, status).

Rotasi password otomatis **belum ada**. Rencananya
`guard db bootstrap-roles --rotate-passwords`, tenggat sebelum M3; prosedur
manual ada di Runbook 8.5 pada [docs/Security_v1.1.md](docs/Security_v1.1.md).

## Tes

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest tests/unit          # tanpa database
uv run pytest                     # termasuk integrasi; butuh docker compose up
```

Tes integrasi hanya mau memakai Postgres dari compose project `payment-guard`
di port 5434. Bila belum jalan, tes gagal dengan pesan yang menyebutkan
perintahnya — ia tidak akan jatuh ke database lain.

## Pre-commit

```bash
uv run pre-commit install
uv run pre-commit run --all-files
```

`gitleaks` memindai rahasia, `detect-private-key` memindai file kunci, `ruff`
memeriksa dan memformat.

## Hashing

Ethereum memakai **Keccak-256**, bukan SHA3-256 yang distandarkan NIST.
Keduanya memakai permutasi Keccak yang sama dengan padding berbeda, jadi
digest-nya berbeda untuk input yang sama. `src/payment_guard/hashing.py` adalah
satu-satunya tempat yang menyentuh backend hash (`pycryptodome`); ada tes yang
menegakkan pembatasan itu, plus known answer untuk keduanya supaya tidak
pernah tertukar. Dipakai mulai M1.

## Cara kerja migrasi

Forward-only, SQL murni di atas psycopg 3, tanpa Alembic maupun SQLAlchemy.

- Satu transaksi per file. Seluruh isi file dikirim sebagai satu string tanpa
  parameter, jadi multi-statement dan blok `$$ … $$` jalan utuh. Gagal di
  tengah file berarti rollback penuh.
- Checksum SHA-256 atas isi file setelah normalisasi CRLF ke LF. File yang
  sudah terapan lalu diedit, diganti nama, atau dihapus menggagalkan runner.
  Perbaikan selalu lewat file baru.
- Nomor harus 1..N berurutan. Celah dan nomor ganda ditolak.
- `pg_advisory_xact_lock` namespace `migrate`: satu runner saja.
- `BEGIN`/`COMMIT` di dalam file ditolak oleh pemeriksaan statik, yang tahu
  bahwa `BEGIN` di dalam `$$ … $$` adalah plpgsql dan bukan transaksi.
- NOTICE atau WARNING berpola "no privileges" **menggagalkan** migrasi: hak
  akses yang diam-diam tidak terpasang adalah mode kegagalan yang berbahaya
  (Security v1.1 E11).

`migrations/0002_roles.sql` hanya memberi hak tingkat tabel dan sequence, dan
gagal keras bila role grup belum ada. Hak tingkat schema dan `CREATE ROLE`
adalah wewenang bootstrap, bukan migrator.

## Security model

> Payment Guard is a testnet-only reference implementation. For an agent
> confined to three tools (`check_payment`, `get_decision_status`,
> `execute_payment`), mandate rules are enforced deterministically. Approvals
> are Ed25519-signed and single-use, bound to the mandate hash that was active
> at approval time, and every on-chain payment is reconciled to the decision
> that allowed it or raised as an alarm. Each process runs with its own
> database role that has only the privileges it needs, and the audit log is
> append-only by trigger and grants.
>
> It does **not** protect against an attacker with access to the same OS user
> (keys are separated by process, configuration, and database role, not by OS
> user). It does not prevent an authorized operator from replacing a mandate,
> though every replacement is audited. Executions already signed or broadcast
> are not cancelled when a mandate changes. The confirmation token in the
> approval screen is an anti-reflex measure, not a security control against the
> agent. The audit log is tamper-evident operationally, not cryptographically.
> The project has not been independently audited. Do not use it with real
> funds.

Rincian lengkap, termasuk risk register RR-1…RR-11, ada di
[docs/Security_v1.1.md](docs/Security_v1.1.md).
