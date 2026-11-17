# Arsitektur

Diagram di bawah menggambarkan **kode yang ada di `src/payment_guard/` saat
dokumen ini ditulis**, bukan rencana. Setiap panah bisa ditunjuk ke modul dan
fungsi yang nyata, dan nomor barisnya disebut di tabel sesudah diagram —
nomor baris itu berlaku untuk commit tempat dokumen ini masuk, dan **nama
fungsinyalah yang mengikat**, bukan nomornya. Yang
**tidak** digambar sebagai alur yang berjalan: pembayaran di testnet publik
Base Sepolia. Chain id 84532 memang dikenali `chains.py`, dan jalur RPC-nya
sama persis dengan yang dipakai devnet, tapi tidak ada bukti bahwa ia pernah
dijalankan di sana — jadi ia muncul sebagai kotak bergaris putus-putus
berlabel "belum (v1.1)". Hal yang sama berlaku untuk hal-hal yang ada di
dokumen tapi belum ada kodenya: tidak digambar sama sekali.

Yang digambar hanyalah jalur pembayaran. Perintah administratif
(`guard db bootstrap-roles`, `guard db migrate`, `guard keygen`) punya
modulnya sendiri dan tidak lewat jalur ini, jadi mereka tidak ada di diagram.

## Alur pembayaran

```mermaid
flowchart TB
    subgraph agent["Agent"]
        A["demo_agent.py<br/>klien MCP, tanpa LLM"]
    end

    subgraph guard["Guard — satu proses per agent"]
        M["mcp_server.py<br/>dua tool: check_payment,<br/>get_decision_status"]
        GS["guard_service.py<br/>start_guard, lock sesi,<br/>Heartbeat fail-closed"]
        DS["decision_service.py<br/>check_payment<br/>satu-satunya penulis decisions"]
        R["rules.py<br/>evaluate, fungsi murni"]
        MD["mandate.py<br/>load_mandate,<br/>compute_mandate_hash"]
        SG["signing.py<br/>build_payload, sign<br/>Ed25519"]
    end

    subgraph human["Operator"]
        AP["approvals_cli.py<br/>approve / reject<br/>hanya interaktif"]
    end

    subgraph exec["Executor"]
        EX["executor.py<br/>cek 1..6, write-ahead,<br/>broadcast, recover"]
        CH["chain.py<br/>encode_transfer, load_wallet,<br/>Web3Client"]
    end

    subgraph obs["Pengamat"]
        W["watcher.py<br/>scan_once, log Transfer<br/>per pengirim"]
        RC["reconciler.py<br/>collect_findings, record"]
    end

    subgraph db["PostgreSQL"]
        T1[("mandates<br/>active_mandates")]
        T2[("decisions")]
        T3[("executions")]
        T4[("settlements<br/>watcher_state")]
        T5[("reconciliations")]
        AU[("audit_log<br/>append-only")]
    end

    NODE["Node RPC<br/>devnet lokal, chain id 31337"]
    BASE["Base Sepolia, chain id 84532<br/>belum (v1.1)"]

    A -- "stdio, dua tool" --> M
    M -- "start_guard" --> GS
    GS -- "muat mandate dari disk,<br/>hitung mandate_hash" --> MD
    GS -- "INSERT mandates,<br/>UPSERT active_mandates" --> T1
    M -- "check_payment" --> DS
    DS -- "build_snapshot lalu evaluate" --> R
    DS -- "INSERT satu baris" --> T2
    DS -- "jika approved otomatis:<br/>tandatangani envelope" --> SG
    T2 -- "status needs_human" --> AP
    AP -- "baca mandate aktif,<br/>cek ulang batas" --> R
    AP -- "UPDATE approved_at,<br/>valid_until, envelope" --> T2
    T2 -- "envelope approved" --> EX
    EX -- "parse dan verifikasi envelope" --> SG
    EX -- "cek 4: baris decisions<br/>harus sama dengan payload" --> T2
    EX -- "fase 1: INSERT signed,<br/>COMMIT sebelum broadcast" --> T3
    EX -- "bangun calldata,<br/>tandatangani lokal" --> CH
    CH -- "eth_sendRawTransaction" --> NODE
    CH -. "jalur sama, belum dijalankan" .-> BASE
    NODE -- "eth_getLogs Transfer<br/>dari wallet Executor" --> W
    W -- "INSERT pending,<br/>lalu final atau reorged" --> T4
    T4 --> RC
    T3 --> RC
    T2 --> RC
    RC -- "UPSERT state" --> T5
    DS -- "audit" --> AU
    AP -- "audit" --> AU
    EX -- "audit" --> AU
    W -- "audit" --> AU
    RC -- "audit" --> AU

    classDef belum stroke-dasharray: 6 4
    class BASE belum
```

### Setiap panah dan sumbernya

| Panah | Modul dan fungsi |
|---|---|
| Agent → MCP | `demo_agent.py`; `mcp_server.py` `TOOL_NAMES` baris 69, `serve` baris 156 |
| MCP → Guard session | `guard_service.py` `start_guard` baris 291; `Heartbeat` baris 384 |
| Guard → mandate | `guard_service.py` `load_mandate_file` baris 190 → `mandate.py` `load_mandate`, `compute_mandate_hash` |
| Guard → `mandates`, `active_mandates` | `guard_service.py` `start_guard`, satu transaksi dengan lock `mandate:<agent_id>` |
| MCP → keputusan | `decision_service.py` `check_payment` baris 378 |
| Keputusan → aturan | `decision_service.py` `build_snapshot` baris 324 → `rules.py` `evaluate` baris 148 |
| Keputusan → `decisions` | `decision_service.py` `_persist` baris 444 |
| Keputusan → envelope | `signing.py` `build_payload`, `sign` |
| `needs_human` → operator | `approvals_cli.py` `fetch_pending` baris 301, `approve` baris 358 |
| Operator → cek ulang | `approvals_cli.py` `_decide` baris 387; `require_interactive` baris 267; `confirm_with_token` baris 281 |
| Operator → `decisions` | `approvals_cli.py` `UPDATE_APPROVED`, menulis `approved_at`, `valid_until`, `envelope` |
| Envelope → Executor | `executor.py` `execute_decision` baris 716 |
| Executor → verifikasi | `executor.py` `parse_and_verify` baris 468 → `signing.py` |
| Executor → cek `decisions` | `executor.py` `_require_identical_decision` baris 530 |
| Executor → `executions` | `executor.py` `_sign_write_ahead` baris 775, commit sebelum broadcast |
| Executor → chain | `executor.py` `_broadcast` baris 840 → `chain.py` `encode_transfer` baris 181, `load_wallet` baris 281 |
| Chain → Watcher | `watcher.py` `scan_once` baris 604, `_scan_range` baris 758 |
| Watcher → `settlements` | `watcher.py` `scan_once`; kursor lewat `read_cursor` baris 569, `write_cursor` baris 583 |
| Tabel → Reconciler | `reconciler.py` `collect_findings` baris 436 |
| Reconciler → `reconciliations` | `reconciler.py` `record` baris 633; `reconcile_once` baris 407 |
| Semua → `audit_log` | `events.py` `audit` baris 301 — **satu-satunya** jalur tulis |

Dua hal yang paling mudah salah dibaca dari diagram, dan karena itu disebut di
sini:

- **Panah Executor → `executions` terjadi sebelum panah Executor → chain, dan
  keduanya transaksi yang berbeda.** `_sign_write_ahead` menulis baris `signed`
  lalu **commit**; broadcast baru sesudahnya, tanpa lock. Kalau prosesnya mati
  di antara keduanya, baris `signed` sudah ada sementara jaringan belum pernah
  melihat transaksinya — itu keadaan yang diselesaikan `recover_pending`
  (`executor.py` baris 914) dengan menyiarkan ulang `raw_tx` yang sama.
- **Panah Node RPC → Watcher menyaring per pengirim, bukan per penerima.**
  Watcher membaca log `Transfer` yang `from`-nya wallet Executor. Akibatnya ia
  juga melihat pembayaran yang **tidak** lewat Guard, dan itu memang yang
  membuat `unmatched_onchain` mungkin.

## Tabel database dan relasinya

```mermaid
erDiagram
    mandates ||--o{ active_mandates : "mandate_hash"
    mandates ||--o{ decisions : "mandate_hash"
    decisions ||--|| executions : "decision_id, UNIQUE"
    decisions ||--o{ reconciliations : "decision_id"
    executions ||--o{ reconciliations : "execution_id"
    settlements ||--o{ reconciliations : "settlement_id"

    mandates {
        text mandate_hash PK
        jsonb content
    }
    active_mandates {
        text agent_id PK
        text mandate_hash FK
    }
    decisions {
        uuid id PK
        text mandate_hash FK
        text status
        jsonb envelope
    }
    executions {
        bigint id PK
        uuid decision_id FK
        text tx_hash
        bigint nonce
        text status
    }
    settlements {
        bigint id PK
        text tx_hash
        int log_index
        text status
    }
    reconciliations {
        bigint id PK
        text subject_type
        text state
    }
    watcher_state {
        int chain_id PK
        bigint last_scanned
    }
    audit_log {
        bigserial id PK
        text actor
        text event
        jsonb payload
    }
```

Tiga hal yang perlu dibaca bersama diagram ini, dan ketiganya ada di
[migrations/0001_init.sql](../migrations/0001_init.sql):

- **`settlements` tidak punya foreign key ke `decisions` maupun
  `executions`.** Itu disengaja: `settlements` adalah catatan apa yang terjadi
  di chain, dan chain tidak tahu apa pun tentang keputusan kami. Yang
  menautkan keduanya adalah `reconciliations`, dan penautan itu bisa **gagal** —
  `unmatched_onchain` justru keadaan ketika tidak ada execution yang cocok.
- **`executions.decision_id` adalah `UNIQUE`.** Satu keputusan tidak bisa punya
  dua execution, dan penjaga terakhirnya ada di skema, bukan di kode.
- **`watcher_state` dan `audit_log` berdiri sendiri.** `watcher_state` hanya
  memuat posisi pemindaian per chain; `audit_log` append-only dengan trigger
  yang menolak `UPDATE`, `DELETE`, dan `TRUNCATE`.
