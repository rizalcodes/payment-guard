# Skema database

**Dokumen turunan. Jangan diedit tangan.**

Dihasilkan dari `migrations/` oleh `scripts/generate_schema_doc.py`.
`tests/unit/test_schema_doc.py` membandingkannya dengan migrasi, jadi
berkas ini tidak bisa basi tanpa tes gagal. Untuk memperbaruinya:

```
uv run --no-sync python scripts/generate_schema_doc.py
```

Sumber kebenaran tetap berkas SQL-nya. Keduanya terkunci: `0001_init.sql`
dan `0002_roles.sql` tidak diubah lagi, dan perubahan skema berjalan lewat
migrasi baru.

Berkas yang dibaca: `0001_init.sql`, `0002_roles.sql`, `0003_watcher_from_addr.sql`.

## Tabel

### `mandates`

Dibuat di `0001_init.sql`.

| Kolom | Tipe dan batasan |
|---|---|
| `mandate_hash` | `TEXT PRIMARY KEY CHECK (mandate_hash ~ '^[0-9a-f]{64}$')` |
| `mandate_id` | `TEXT NOT NULL CHECK (char_length(mandate_id) BETWEEN 1 AND 64)` |
| `agent_id` | `TEXT NOT NULL CHECK (agent_id ~ '^[a-z0-9][a-z0-9_-]{0,63}$')` |
| `content` | `JSONB NOT NULL` |
| `first_seen_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` |

Trigger:

- `mandates_no_change`: BEFORE UPDATE OR DELETE
- `mandates_no_truncate`: BEFORE TRUNCATE

### `active_mandates`

Dibuat di `0001_init.sql`.

| Kolom | Tipe dan batasan |
|---|---|
| `agent_id` | `TEXT PRIMARY KEY CHECK (agent_id ~ '^[a-z0-9][a-z0-9_-]{0,63}$')` |
| `mandate_hash` | `TEXT NOT NULL REFERENCES mandates(mandate_hash)` |
| `activated_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` |

### `decisions`

Dibuat di `0001_init.sql`.

| Kolom | Tipe dan batasan |
|---|---|
| `id` | `UUID PRIMARY KEY` |
| `agent_id` | `TEXT NOT NULL CHECK (agent_id ~ '^[a-z0-9][a-z0-9_-]{0,63}$')` |
| `mandate_hash` | `TEXT NOT NULL REFERENCES mandates(mandate_hash)` |
| `chain_id` | `INT NOT NULL CHECK (chain_id > 0)` |
| `recipient` | `TEXT NOT NULL CHECK (recipient ~ '^0x[0-9a-f]{40}$')` |
| `amount_base` | `NUMERIC(78,0) NOT NULL CHECK (amount_base > 0)` |
| `reason_text` | `TEXT CHECK (char_length(reason_text) <= 500)` |
| `status` | `TEXT NOT NULL CHECK (status IN ('approved','denied','needs_human','rejected_by_human','expired'))` |
| `reason_code` | `TEXT NOT NULL CHECK (reason_code IN ( 'approved','approved_by_human', 'retry_blocked','recipient_not_allowed', 'over_per_tx_limit','over_daily_limit', 'recheck_over_per_tx_limit','recheck_over_daily_limit', 'mandate_changed', 'human_approval_required', 'human_rejected', 'human_wait_timeout'))` |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` |
| `decided_at` | `TIMESTAMPTZ` |
| `approved_at` | `TIMESTAMPTZ` |
| `valid_until` | `TIMESTAMPTZ` |
| `expires_at` | `TIMESTAMPTZ` |
| `envelope` | `JSONB` |

CHECK bernama:

- `status_reason_pairs`: `CHECK ( (status = 'approved' AND reason_code IN ('approved','approved_by_human')) OR (status = 'denied' AND reason_code IN ('retry_blocked','recipient_not_allowed', 'over_per_tx_limit','over_daily_limit', 'recheck_over_per_tx_limit','recheck_over_daily_limit', 'mandate_changed')) OR (status = 'needs_human' AND reason_code = 'human_approval_required') OR (status = 'rejected_by_human' AND reason_code = 'human_rejected') OR (status = 'expired' AND reason_code = 'human_wait_timeout') )`
- `approved_is_complete`: `CHECK ( (status = 'approved' AND approved_at IS NOT NULL AND valid_until IS NOT NULL AND envelope IS NOT NULL AND decided_at IS NOT NULL AND approved_at = decided_at) OR (status <> 'approved' AND approved_at IS NULL AND valid_until IS NULL AND envelope IS NULL) )`
- `decision_state_timestamps`: `CHECK ( (status = 'needs_human' AND decided_at IS NULL AND expires_at IS NOT NULL) OR (status = 'expired' AND expires_at IS NOT NULL AND decided_at = expires_at) OR (status NOT IN ('needs_human','expired') AND decided_at IS NOT NULL) )`
- `valid_until_after_approval`: `CHECK ( valid_until IS NULL OR valid_until > approved_at )`
- `expires_after_creation`: `CHECK ( expires_at IS NULL OR expires_at > created_at )`
- `decided_not_before_created`: `CHECK ( decided_at IS NULL OR decided_at >= created_at )`
- `decided_within_expiry`: `CHECK ( expires_at IS NULL OR decided_at IS NULL OR decided_at <= expires_at )`
- `approved_within_expiry`: `CHECK ( expires_at IS NULL OR approved_at IS NULL OR approved_at <= expires_at )`

### `executions`

Dibuat di `0001_init.sql`.

| Kolom | Tipe dan batasan |
|---|---|
| `id` | `BIGSERIAL PRIMARY KEY` |
| `decision_id` | `UUID NOT NULL UNIQUE REFERENCES decisions(id)` |
| `tx_hash` | `TEXT NOT NULL UNIQUE CHECK (tx_hash ~ '^0x[0-9a-f]{64}$')` |
| `from_addr` | `TEXT NOT NULL CHECK (from_addr ~ '^0x[0-9a-f]{40}$')` |
| `to_addr` | `TEXT NOT NULL CHECK (to_addr ~ '^0x[0-9a-f]{40}$')` |
| `amount_base` | `NUMERIC(78,0) NOT NULL CHECK (amount_base > 0)` |
| `nonce` | `BIGINT NOT NULL CHECK (nonce >= 0)` |
| `raw_tx` | `BYTEA NOT NULL` |
| `status` | `TEXT NOT NULL CHECK (status IN ('signed','broadcast','confirmed','failed'))` |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` |
| `broadcast_at` | `TIMESTAMPTZ` |
| `updated_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` |

Unik:

- `UNIQUE (from_addr, nonce)`

CHECK bernama:

- `broadcast_has_timestamp`: `CHECK ( status = 'signed' OR status = 'failed' OR broadcast_at IS NOT NULL )`

### `settlements`

Dibuat di `0001_init.sql`.

| Kolom | Tipe dan batasan |
|---|---|
| `id` | `BIGSERIAL PRIMARY KEY` |
| `chain_id` | `INT NOT NULL CHECK (chain_id > 0)` |
| `tx_hash` | `TEXT NOT NULL CHECK (tx_hash ~ '^0x[0-9a-f]{64}$')` |
| `log_index` | `INT NOT NULL CHECK (log_index >= 0)` |
| `block_number` | `BIGINT NOT NULL CHECK (block_number >= 0)` |
| `block_hash` | `TEXT NOT NULL CHECK (block_hash ~ '^0x[0-9a-f]{64}$')` |
| `from_addr` | `TEXT NOT NULL CHECK (from_addr ~ '^0x[0-9a-f]{40}$')` |
| `to_addr` | `TEXT NOT NULL CHECK (to_addr ~ '^0x[0-9a-f]{40}$')` |
| `amount_base` | `NUMERIC(78,0) NOT NULL CHECK (amount_base >= 0)` |
| `status` | `TEXT NOT NULL CHECK (status IN ('pending','final','reorged'))` |
| `first_seen_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` |
| `finalized_at` | `TIMESTAMPTZ` |
| `last_verified_at` | `TIMESTAMPTZ` |

Unik:

- `UNIQUE (chain_id, tx_hash, log_index)`

CHECK bernama:

- `final_has_timestamp`: `CHECK ( (status = 'final' AND finalized_at IS NOT NULL) OR status <> 'final' )`

### `reconciliations`

Dibuat di `0001_init.sql`.

| Kolom | Tipe dan batasan |
|---|---|
| `id` | `BIGSERIAL PRIMARY KEY` |
| `subject_type` | `TEXT NOT NULL CHECK (subject_type IN ('settlement','execution','decision'))` |
| `subject_id` | `TEXT NOT NULL` |
| `state` | `TEXT NOT NULL CHECK (state IN ('matched','unmatched_onchain','mismatch', 'executed_not_settled','expired','invalidated'))` |
| `decision_id` | `UUID REFERENCES decisions(id)` |
| `execution_id` | `BIGINT REFERENCES executions(id)` |
| `settlement_id` | `BIGINT REFERENCES settlements(id)` |
| `detail` | `JSONB` |
| `updated_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` |

Unik:

- `UNIQUE (subject_type, subject_id)`

### `watcher_state`

Dibuat di `0001_init.sql`.

| Kolom | Tipe dan batasan |
|---|---|
| `chain_id` | `INT PRIMARY KEY CHECK (chain_id > 0)` |
| `last_scanned` | `BIGINT NOT NULL CHECK (last_scanned >= 0)` |

### `audit_log`

Dibuat di `0001_init.sql`.

| Kolom | Tipe dan batasan |
|---|---|
| `id` | `BIGSERIAL PRIMARY KEY` |
| `ts` | `TIMESTAMPTZ NOT NULL DEFAULT now()` |
| `actor` | `TEXT NOT NULL CHECK (actor IN ('guard','approver','executor','watcher','reconciler','human','migrator'))` |
| `event` | `TEXT NOT NULL CHECK (char_length(event) BETWEEN 1 AND 64)` |
| `payload` | `JSONB NOT NULL` |

Trigger:

- `audit_log_no_change`: BEFORE UPDATE OR DELETE
- `audit_log_no_truncate`: BEFORE TRUNCATE

## Hak akses, ringkas

Hak diberikan ke role **grup** (NOLOGIN); role LOGIN per proses
mewarisinya lewat keanggotaan. Tidak ada DELETE, TRUNCATE, REFERENCES,
atau TRIGGER untuk role mana pun.

| Objek | Role | Hak |
|---|---|---|
| `active_mandates` | `approver_rw` | `SELECT` |
| `active_mandates` | `executor_rw` | `SELECT` |
| `active_mandates` | `guard_rw` | `SELECT, INSERT, UPDATE` |
| `audit_log` | `approver_rw` | `SELECT, INSERT` |
| `audit_log` | `executor_rw` | `INSERT` |
| `audit_log` | `guard_rw` | `SELECT, INSERT` |
| `audit_log` | `reconciler_rw` | `SELECT, INSERT` |
| `audit_log` | `watcher_rw` | `INSERT` |
| `audit_log_id_seq` | `approver_rw` | `USAGE` |
| `audit_log_id_seq` | `executor_rw` | `USAGE` |
| `audit_log_id_seq` | `guard_rw` | `USAGE` |
| `audit_log_id_seq` | `reconciler_rw` | `USAGE` |
| `audit_log_id_seq` | `watcher_rw` | `USAGE` |
| `decisions` | `approver_rw` | `SELECT` |
| `decisions` | `approver_rw` | `UPDATE (status, reason_code, decided_at, approved_at, valid_until, envelope)` |
| `decisions` | `executor_rw` | `SELECT` |
| `decisions` | `guard_rw` | `SELECT, INSERT` |
| `decisions` | `guard_rw` | `UPDATE (status, reason_code, decided_at)` |
| `decisions` | `reconciler_rw` | `SELECT` |
| `executions` | `approver_rw` | `SELECT` |
| `executions` | `executor_rw` | `SELECT, INSERT` |
| `executions` | `executor_rw` | `UPDATE (status, broadcast_at, updated_at)` |
| `executions` | `guard_rw` | `SELECT` |
| `executions` | `reconciler_rw` | `SELECT` |
| `executions` | `watcher_rw` | `SELECT (from_addr)` |
| `executions_id_seq` | `executor_rw` | `USAGE` |
| `mandates` | `approver_rw` | `SELECT` |
| `mandates` | `executor_rw` | `SELECT` |
| `mandates` | `guard_rw` | `SELECT, INSERT` |
| `mandates` | `reconciler_rw` | `SELECT` |
| `reconciliations` | `reconciler_rw` | `SELECT, INSERT` |
| `reconciliations` | `reconciler_rw` | `UPDATE (state, detail, updated_at)` |
| `reconciliations_id_seq` | `reconciler_rw` | `USAGE` |
| `settlements` | `reconciler_rw` | `SELECT` |
| `settlements` | `watcher_rw` | `SELECT, INSERT` |
| `settlements` | `watcher_rw` | `UPDATE (status, block_number, block_hash, finalized_at, last_verified_at)` |
| `settlements_id_seq` | `watcher_rw` | `USAGE` |
| `watcher_state` | `watcher_rw` | `SELECT, INSERT, UPDATE` |

`schema_migrations` tidak muncul di tabel itu dengan sengaja: hanya
pemilik objek yang boleh menyentuhnya.
