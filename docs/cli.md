<!-- Referensi CLI. Tiap entri diperiksa terhadap --help di HEAD saat dokumen ini ditulis. -->

# Referensi CLI

Satu entry point: `guard`, dari `[project.scripts]` di `pyproject.toml` baris
20 → `payment_guard.cli:main`. Tidak ada perintah lain, dan tidak ada
perintah di dokumen ini yang belum ada di kode.

Setiap flag di bawah diambil dari keluaran `--help` yang sungguh dijalankan,
bukan dari ingatan. Setiap kode keluar diambil dari `return` di kode, dengan
nomor barisnya. Nomor baris berlaku untuk commit tempat dokumen ini masuk;
**nama fungsinya yang mengikat**, bukan nomornya.

Kolom "pernah dijalankan di devnet" menyebut apakah perintah itu benar-benar
pernah dipakai di devnet lokal selama skenario (a), (e), (d), dan (b).
"Belum" bukan berarti rusak — hanya berarti tidak ada bukti pemakaiannya.

## Pohon perintah

```
guard
├── db
│   ├── migrate
│   ├── status
│   └── bootstrap-roles
├── keygen
├── serve
├── approvals
│   ├── list
│   ├── approve
│   └── reject
├── execute
├── recover
├── watch
├── reconcile
├── wallet
└── verify
```

Perintah akar sendiri:

```
usage: guard [-h] [--version]
             {db,keygen,serve,approvals,execute,recover,watch,reconcile,wallet,verify}
             ...

  --version             show program's version number and exit
```

`--version` dideklarasikan di `build_parser`, `src/payment_guard/cli.py`
baris 290.

## Kode keluar bersama

Berlaku untuk semua perintah, dari `main` di `src/payment_guard/cli.py`
baris 583:

| Kode | Arti | Sumber |
|---|---|---|
| `0` | berhasil | `return int(args.handler(args))` baris 606 |
| `1` | `MigrationError` atau `KeygenError`; pesannya dicetak ke stderr | baris 607-610 |
| `130` | `KeyboardInterrupt`, yaitu Ctrl-C | baris 611-612 |
| `2` | argparse menolak argumen; tidak ditulis kode kami | argparse |

Perintah yang punya kode keluar bermakna sendiri disebut di entrinya.

---

## `guard db migrate`

Terapkan migrasi yang belum pernah diterapkan, forward-only.

```
usage: guard db migrate [-h] [--migrations-dir MIGRATIONS_DIR]
                        [--migrator-env-file MIGRATOR_ENV_FILE]

  --migrations-dir MIGRATIONS_DIR
                        direktori file migrasi (default: migrations/ di akar repo)
  --migrator-env-file MIGRATOR_ENV_FILE
                        file berisi MIGRATOR_DB_URL (default: .env.migrator di akar repo)
```

Kode keluar: `0` selalu bila tidak ada pengecualian — `_cmd_migrate` di
`cli.py` baris 60, `return 0` baris 68. Kegagalan migrasi muncul sebagai
`MigrationError` dan jadi `1` di `main`.

Pernah dijalankan di devnet: **ya**, lewat `scripts/devnet_up.py`
(`migrate_command`) saat devnet dibangun. Keluaran mentahnya tidak disimpan
terpisah; buktinya skema devnet ada dan suite integrasi hijau.

## `guard db status`

Laporkan migrasi mana yang sudah dan belum diterapkan.

```
usage: guard db status [-h] [--migrations-dir MIGRATIONS_DIR]
                       [--migrator-env-file MIGRATOR_ENV_FILE]
```

Flagnya sama dengan `migrate`. Kode keluar: `0`, dari `_cmd_status` di
`cli.py` baris 71, `return 0` baris 80.

Pernah dijalankan di devnet: **belum**.

## `guard db bootstrap-roles`

Buat role database. Dijalankan admin, sekali.

```
usage: guard db bootstrap-roles [-h] [--env-file ENV_FILE] [--no-prompt]
                                [--allow-remote-admin]
                                [--migrator-env-file MIGRATOR_ENV_FILE]

  --env-file ENV_FILE   file URL runtime, lima proses (default: .env di akar
                        repo). Tidak memuat URL migrator.
  --no-prompt           jangan meminta URL admin lewat prompt; wajib lewat
                        environment variable
  --allow-remote-admin  izinkan URL admin yang hostnya bukan loopback. Secara
                        bawaan ditolak: bootstrap membuat role dan mengubah
                        hak tingkat schema.
  --migrator-env-file MIGRATOR_ENV_FILE
                        file berisi MIGRATOR_DB_URL (default: .env.migrator di
                        akar repo)
```

Kode keluar: `0`, dari `_cmd_bootstrap_roles` di `cli.py` baris 83, `return 0`
baris 96.

Pernah dijalankan di devnet: **ya**, lewat `scripts/devnet_up.py`
(`bootstrap_command`), dengan `--no-prompt` dan **tanpa**
`--allow-remote-admin` — URL admin devnet selalu loopback, dan membiarkan flag
itu lewat berarti pengaman host hilang tanpa ada yang memintanya.

## `guard keygen`

Buat pasangan kunci Ed25519 untuk tanda tangan Guard. Tidak pernah menimpa
yang sudah ada.

```
usage: guard keygen [-h] [--signing-key-file SIGNING_KEY_FILE]
                    [--public-keys-file PUBLIC_KEYS_FILE]

  --signing-key-file SIGNING_KEY_FILE
                        tujuan bahan kunci rahasia (default:
                        keys/guard_signing.json di akar repo). Di-gitignore;
                        jangan di-commit.
  --public-keys-file PUBLIC_KEYS_FILE
                        registri kunci publik (default: guard_public.json di
                        akar repo). Boleh di-commit.
```

Kode keluar: `0`, dari `_cmd_keygen` di `cli.py` baris 99, `return 0` baris
103. Menimpa kunci yang sudah ada menghasilkan `KeygenError` → `1`.

Pernah dijalankan di devnet: **ya**, lewat `scripts/devnet_up.py`
(`keygen_command`), dengan kedua path diarahkan ke luar repo.

## `guard serve`

Jalankan Guard sebagai server MCP stdio. Tepat dua tool: `check_payment` dan
`get_decision_status` (`mcp_server.py` `TOOL_NAMES` baris 69).

```
usage: guard serve [-h] [--env-file ENV_FILE]

  --env-file ENV_FILE  file konfigurasi runtime Guard (default: .env di akar
                       repo). Tidak pernah membaca .env.migrator.
```

Kode keluar: diteruskan dari `serve` di `mcp_server.py` baris 156 —
`_cmd_serve` di `cli.py` baris 106 memakai `return serve(...)` baris 114.
`serve` mengembalikan `1` pada dua jalur kegagalan start (baris 172 dan 178).

Pernah dijalankan di devnet: **belum**. Keputusan devnet dibuat dengan
memanggil `decision_service.check_payment` langsung lewat skrip, bukan lewat
server MCP.

## `guard approvals list`

Tampilkan keputusan yang menunggu persetujuan manusia.

```
usage: guard approvals list [-h]
```

Tidak punya flag sendiri. `--env-file` milik grup `approvals`, **bukan**
subperintahnya — lihat catatan di bawah.

Kode keluar: `0`, dari `list_pending` di `approvals_cli.py` baris 327
(`return 0` baris 333 dan 344).

Pernah dijalankan di devnet: **ya**, sebagai pemeriksaan baca-saja sebelum
approval.

## `guard approvals approve` dan `guard approvals reject`

```
usage: guard approvals approve [-h] decision_id
usage: guard approvals reject [-h] decision_id

positional arguments:
  decision_id  UUID dari jawaban check_payment
```

**Tidak ada flag untuk melewati konfirmasi.** Tidak ada `--yes`, tidak ada
mode non-interaktif: `require_interactive` (`approvals_cli.py` baris 267)
menolak stdin yang bukan terminal, dan `confirm_with_token` (baris 281)
menuntut token 6 karakter diketik ulang.

Kode keluar: `1` bila konfigurasi ditolak (`cli.py` baris 133) atau
`ApprovalError` dilempar (baris 142); `0` bila selesai (baris 143).

Pernah dijalankan di devnet: `approve` **ya** — lima kali, diketik pemilik
repo di terminal sungguhan. Keluarannya tidak tersimpan di scratchpad sesi
ini karena bukan saya yang menjalankannya; buktinya lima kejadian
`approval_approved` beraktor `human` di `audit_log`. `reject` **belum**.

### Letak `--env-file` di `approvals`

```
usage: guard approvals [-h] [--env-file ENV_FILE] {list,approve,reject} ...

  --env-file ENV_FILE   file konfigurasi runtime (default: .env di akar repo).
                        Memakai APPROVER_DB_URL, bukan URL Guard.
```

Flag itu didaftarkan ke parser `approvals` (`cli.py` baris 399), bukan ke
subperintahnya, jadi letaknya **sebelum** subperintah:

```
guard approvals --env-file .env.devnet approve <uuid>     # benar
guard approvals approve --env-file .env.devnet <uuid>     # flag tak dikenal
```

Itu berbeda dari `execute` dan `recover`, yang mendeklarasikan `--env-file`
pada dirinya sendiri (`cli.py` baris 441).

## `guard execute`

Eksekusi satu keputusan `approved` ke chain: write-ahead lalu broadcast.
Receipt **tidak** ditunggu.

```
usage: guard execute [-h] [--env-file ENV_FILE] decision_id

positional arguments:
  decision_id          UUID keputusan approved yang belum punya execution

  --env-file ENV_FILE  file konfigurasi runtime (default: .env di akar repo).
                       Memakai EXECUTOR_DB_URL, EXECUTOR_KEY_PATH, dan RPC_URL.
```

Kode keluar, dari `run_execute` di `executor.py` baris 1114:
`return 0 if outcome.status != OUTCOME_REJECTED else 1` baris 1132. Jadi **`1`
berarti salah satu cek 1..6 menolak**, bukan kesalahan teknis. `1` juga
dipakai `cli.py` untuk konfigurasi yang ditolak (baris 158) dan untuk
`ChainError`/`ExecutorError` (baris 164).

Pernah dijalankan di devnet: **ya** — `guard execute --env-file .env.devnet <uuid>`.

## `guard recover`

Selesaikan execution yang tertinggal berstatus `signed` atau `broadcast`.
Idempoten.

```
usage: guard recover [-h] [--env-file ENV_FILE]

  --env-file ENV_FILE  file konfigurasi runtime (default: .env di akar repo).
                       Memakai EXECUTOR_DB_URL, EXECUTOR_KEY_PATH, dan RPC_URL.
```

Kode keluar: `0`, dari `run_recover` di `executor.py` baris 1135 (`return 0`
baris 1145). `1` hanya dari `cli.py` untuk konfigurasi yang ditolak (baris
176) atau `ChainError`/`ExecutorError` (baris 182).

**Butuh dua kali untuk baris `signed`.** `signed → broadcast` di pemanggilan
pertama, `broadcast → confirmed` di pemanggilan kedua; yang ketiga baru
no-op. Itu bukan kegagalan, melainkan akibat `_recover_one` keluar tepat
setelah menyiarkan ulang.

Pernah dijalankan di devnet: **ya** — termasuk tiga kali berturut-turut untuk
membuktikan idempotensinya.

## `guard watch`

Pindai log `Transfer` USDC dari wallet Executor menjadi `settlements`.

```
usage: guard watch [-h] [--loop] [--cycles N] [--from-block BLOK]
                   [--env-file ENV_FILE]

  --loop               jalankan terus dengan jeda WATCHER_INTERVAL_SECONDS.
                       Tanpa ini, satu siklus lalu keluar.
  --cycles N           batasi jumlah siklus (dipakai operator untuk mencoba,
                       dan oleh tes)
  --from-block BLOK    blok awal, HANYA bila belum ada kursor di
                       watcher_state. Kursor yang sudah ada tidak pernah
                       dipindahkan ke depan oleh flag ini: itu akan
                       melewatkan blok dalam diam. Lihat docs/runbook.md
                       bagian 8.
  --env-file ENV_FILE  file konfigurasi runtime (default: .env di akar repo).
                       Memakai WATCHER_DB_URL, RPC_URL, dan
                       EXECUTOR_WALLET_ADDRESS.
```

Kode keluar, dari `run_watch` di `watcher.py` baris 1246:
`return 1 if alarms else 0` baris 1286. **`1` berarti ada alarm**, yaitu
settlement yang pernah `final` lalu ter-reorg — bukan kegagalan teknis.
`cli.py` memakai `1` juga untuk konfigurasi yang ditolak (baris 195) dan
`ChainError`/`WatcherError` (baris 213).

Pernah dijalankan di devnet: **ya** — `--cycles 1` berkali-kali, dan sekali
dengan `--from-block 0` saat `watcher_state` masih kosong. `--loop` **belum**
dijalankan di devnet.

**Pembayaran normal butuh dua siklus.** Settlement baru disisipkan ketika
bloknya sudah melewati `safe_head` = `latest - CONFIRMATIONS`, lalu
difinalkan di siklus berikutnya.

## `guard reconcile`

Cocokkan `settlements` dengan `executions` dan `decisions`; satu siklus.

```
usage: guard reconcile [-h] [--report] [--env-file ENV_FILE]

  --report             hanya tampilkan laporan, jangan tulis apa pun.
                       Menghitung ulang dari settlements, executions, dan
                       decisions; tidak menulis ke reconciliations maupun
                       audit_log.
  --env-file ENV_FILE  file konfigurasi runtime (default: .env di akar repo).
                       Memakai RECONCILER_DB_URL saja; tidak ada RPC_URL.
```

Kode keluar: `return 1 if report.alarms else 0` — di `run_report`
(`reconciler.py` baris 396, `return` baris 404) **dan** di `run_reconcile`
(baris 688, `return` baris 700). Konvensinya sengaja sama untuk kedua mode.

**`1` berarti ada keadaan yang perlu tindakan**, yaitu `unmatched_onchain`,
`mismatch`, atau `invalidated` (`ALARM_STATES` di `reconciler.py` baris 74) —
bukan kegagalan teknis. Pemantauan sebaiknya membaca kode keluar ini, bukan
mencari kejadian bernama `alarm_*` di `audit_log`: reconciler tidak pernah
menulis nama seperti itu. Lihat `docs/runbook.md` bagian 11.

Pernah dijalankan di devnet: **ya**, kedua mode. Kode keluar `1` terbukti
muncul pada skenario (b), dengan satu `unmatched_onchain`.

## `guard wallet`

Cetak alamat wallet Executor dari berkas kuncinya. Tanpa database, tanpa
jaringan, tanpa transaksi.

```
usage: guard wallet [-h] [--env-file ENV_FILE]

  --env-file ENV_FILE  file konfigurasi runtime (default: .env di akar repo).
                       Hanya EXECUTOR_KEY_PATH yang dibaca.
```

Perintah ini **sengaja** tidak punya argumen lain — komentar di `cli.py`
baris 524-527 menyebut alasannya: perintah yang hanya bisa melakukan satu hal
tidak bisa disuruh melakukan hal lain.

Kode keluar: `0` baris 277; `1` pada dua jalur kegagalan pembacaan kunci
(baris 263 dan 271).

Pernah dijalankan di devnet: **belum**. Alamat wallet dibaca dari laporan
start `guard execute`.

## `guard verify`

Periksa envelope hanya dengan kunci publik. Tanpa database dan tanpa
jaringan.

```
usage: guard verify [-h] [--pubkey PUBKEY] [--mandate MANDATE]
                    [--network {base-sepolia,devnet}]
                    envelope

positional arguments:
  envelope              berkas envelope JSON

  --pubkey PUBKEY       registri kunci publik (default: guard_public.json di
                        akar repo)
  --mandate MANDATE     opsional: cocokkan mandate_hash di payload dengan hash
                        berkas mandate ini, supaya envelope yang sah atas
                        aturan yang salah tidak lolos
  --network {base-sepolia,devnet}
                        jaringan yang envelope-nya diperiksa (default:
                        base-sepolia). Pakai 'devnet' untuk envelope dari
                        devnet lokal (chain id 31337); tanpa itu, envelope
                        devnet ditolak karena chain_id-nya bukan 84532. Lihat
                        docs/devnet.md.
```

Kode keluar, dari `_cmd_verify` di `cli.py` baris 280: `0` bila envelope sah
(baris 286), `1` bila tidak (baris 287).

`verify` **tidak** memeriksa `valid_until`. Pemisahan itu disengaja:
`check_not_expired` (`signing.py` baris 513) butuh jam database, dan `verify`
bekerja tanpa database. Executor yang memakainya, di cek 3b.

Pernah dijalankan di devnet: **belum**. `--network devnet` ada di kode dan
diuji suite, tapi tidak ada bukti pemakaiannya atas envelope devnet.
