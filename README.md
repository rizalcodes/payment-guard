# Payment Guard

Reference implementation: penegakan aturan mandate yang deterministik untuk
pembayaran yang diminta agent. **Testnet only. Bukan audit, bukan production,
bukan untuk uang asli.**

Dokumen yang mengikat: [docs/PRD_v2.1.md](docs/PRD_v2.1.md),
[docs/Architecture_v1.1.md](docs/Architecture_v1.1.md),
[docs/Security_v1.1.md](docs/Security_v1.1.md). Sumber kebenaran skema:
[migrations/0001_init.sql](migrations/0001_init.sql) dan
[migrations/0002_roles.sql](migrations/0002_roles.sql).

Status: **M5**. Yang sudah ada: struktur repo, runner migrasi, role database,
aturan mandate, envelope bertanda tangan, Guard sebagai server MCP, Approvals
CLI, agent demo, dan Executor dengan write-ahead serta recovery. Watcher dan
Reconciler menyusul di M6 dan sesudahnya. README lengkap adalah pekerjaan M9.

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

## Menjalankan Executor

Executor memegang **kunci wallet-nya sendiri** dan hanya **kunci publik** Guard.
Ia tidak pernah membaca `GUARD_SIGNING_KEY_PATH`; ada tes yang memeriksa bahwa
nama variabel itu tidak muncul di modulnya sama sekali. `EXECUTOR_KEY_PATH`
tunduk pada aturan yang sama seperti kunci Guard: **harus di luar akar repo**.

```bash
uv run guard execute <decision_id>   # satu keputusan approved, write-ahead lalu broadcast
uv run guard recover                 # selesaikan yang tertinggal. Idempoten.
```

Saat start, Executor memanggil `decimals()` pada kontrak USDC dan **menolak
jalan** bila hasilnya bukan 6, serta mencocokkan `eth_chainId` dengan 84532.
Saldo ETH yang kurang hanya memberi peringatan, bukan menolak start: saldo gas
yang rendah tidak boleh menghalangi `guard recover` justru saat ia dibutuhkan.

`execute` menulis baris `executions` berstatus `signed` beserta `tx_hash`,
`nonce`, dan `raw_tx`, dan **meng-commit-nya sebelum broadcast**. Receipt tidak
ditunggu; `recover` yang menyelesaikannya. `recover` menyiarkan ulang `raw_tx`
yang sama persis, jadi menjalankannya berulang kali tidak bisa menghasilkan
pembayaran kedua. Status `failed` hanya ditulis bila receipt revert atau nonce-nya
dipakai transaksi lain — **tidak pernah** karena timeout.

Tidak ada flag konfirmasi di sini dan tidak ada `--yes`. Yang menyetujui
pembayaran adalah operator di `guard approvals approve`; `execute` hanya
memproses keputusan `approved` yang belum punya execution.

Panduan demo satu pembayaran dari ujung ke ujung, termasuk faucet dan alamat
jaringan beserta sumber verifikasinya, ada di
[scripts/demo_execute.md](scripts/demo_execute.md).

## Perintah yang tersedia sampai M5

| Perintah | Dijalankan sebagai | Membaca | Guna |
|---|---|---|---|
| `guard db bootstrap-roles` | admin, sekali | env sementara / prompt | Role, keanggotaan grup, hak tingkat schema. Menulis `.env` dan `.env.migrator` |
| `guard db migrate` | migrator | `.env.migrator` | Terapkan migrasi yang belum terapan |
| `guard db status` | migrator | `.env.migrator` | Laporkan migrasi terapan dan tertunda |
| `guard keygen` | operator, sekali | — | Pasangan kunci Ed25519. Tidak pernah menimpa yang sudah ada |
| `guard serve` | host agent | `.env` | Guard sebagai server MCP stdio, dua tool |
| `guard approvals list` | operator | `.env` | Keputusan yang menunggu persetujuan manusia |
| `guard approvals approve` | operator, interaktif | `.env` | Setujui satu keputusan; batas dicek ulang |
| `guard approvals reject` | operator, interaktif | `.env` | Tolak satu keputusan; memicu cooldown |
| `guard execute` | host agent / operator | `.env` | Eksekusi satu keputusan approved: cek 1–6, write-ahead, broadcast |
| `guard recover` | operator | `.env` | Selesaikan execution `signed` atau `broadcast` yang tertinggal |
| `guard verify` | operator | — | Periksa envelope dengan kunci publik saja |

`approvals` memakai `APPROVER_DB_URL` dari `.env`, bukan URL Guard: hanya role
approver yang boleh menulis `approved_at`, `valid_until`, dan `envelope`.
`approve` dan `reject` **menolak jalan di luar terminal interaktif** dan tidak
punya flag untuk melewatinya (Security v1.1 E3, ST-23).

`keygen` dan `verify` tidak membaca file env sama sekali: keduanya bekerja atas
file yang diberikan di baris perintah. `verify` memang dirancang jalan **tanpa
database dan tanpa jaringan**, dan tidak punya jalur untuk membaca bahan kunci
rahasia — lihat E2 di [docs/Security_v1.1.md](docs/Security_v1.1.md).

Flag per perintah:

| Perintah | Flag |
|---|---|
| `db bootstrap-roles` | `--env-file`, `--migrator-env-file`, `--no-prompt`, `--allow-remote-admin` |
| `db migrate` | `--migrations-dir`, `--migrator-env-file` |
| `db status` | `--migrations-dir`, `--migrator-env-file` |
| `keygen` | `--signing-key-file`, `--public-keys-file` |
| `serve` | `--env-file` |
| `approvals list` | `--env-file` |
| `approvals approve` | `<decision_id>` (posisional), `--env-file` |
| `approvals reject` | `<decision_id>` (posisional), `--env-file` |
| `execute` | `<decision_id>` (posisional), `--env-file` |
| `recover` | `--env-file` |
| `verify` | `<envelope>` (posisional), `--pubkey`, `--mandate` |

`execute` dan `recover` memakai `EXECUTOR_DB_URL`, `EXECUTOR_KEY_PATH`, dan
`RPC_URL` dari `.env`. `RPC_URL` **rahasia** bila Anda memakai penyedia
berbayar, karena kunci API biasa diletakkan di path atau query-nya; Executor
tidak pernah mencetaknya utuh, hanya skema dan host-nya.

`guard --version` ada di tingkat atas. Perintah runtime lainnya (`watch`,
`reconcile`) menyusul di milestone berikutnya.

Rotasi password otomatis **belum diimplementasi**. Rencananya
`guard db bootstrap-roles --rotate-passwords`, ditinjau di M9 bersama perubahan
skema lain. Sampai itu, rotasi dilakukan manual; lihat Runbook 8.5 pada
[docs/Security_v1.1.md](docs/Security_v1.1.md).

## Tes

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest tests/unit          # tanpa database

# Tes integrasi: cluster TES terpisah, bukan cluster Anda di 5434.
docker compose --profile test up -d postgres_test
cp .env.test.example .env.test    # berisi URL cluster tes; di-gitignore
uv run pytest
```

**Tes integrasi tidak bisa menyentuh cluster Anda, dan itu ditegakkan.** Harness
menolak jalan kecuali nama databasenya berakhiran `_test`, hostnya loopback, dan
portnya ditulis eksplisit serta **bukan 5434**. Tidak ada URL bawaan: ia hanya
datang dari `PAYMENT_GUARD_TEST_ADMIN_DB_URL` atau `.env.test`, dan harness tidak
pernah membaca `.env` maupun `.env.migrator`.

Itu bukan kehati-hatian teoretis. Role Postgres bersifat **cluster-wide**, bukan
per database, dan harness membuat serta menghapus role. Versi sebelumnya punya
URL bawaan yang menunjuk tepat ke database operator; satu run tes cukup untuk
menghapus role `*_app` dan membuat `.env` berhenti cocok. Nama role login di tes
kini juga ber-awalan acak per tes (`t<8 hex>_guard_app`), sehingga dua run tidak
bisa berebut nama.

Role **grup** (`guard_rw` dan seterusnya) tetap tanpa awalan: namanya tertulis
harfiah di `migrations/0002_roles.sql`, yang terkunci. Role itu NOLOGIN, tanpa
password, dan tidak muncul di file env mana pun.

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
