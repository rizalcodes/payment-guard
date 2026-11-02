-- Payment Guard — 0001_init.sql (revisi, selaras PRD v2.1 / Arsitektur v1.1 / Security v1.1)
--
-- ATURAN FILE:
--   * TANPA BEGIN/COMMIT. Runner migrasi yang memegang transaksi (atomik per file).
--   * Semua jumlah uang = bilangan bulat base units (USDC 6 desimal, 1 USDC = 1_000_000).
--     NUMERIC(78,0) membulatkan desimal secara diam-diam; kode WAJIB hanya mengirim int.
--   * Alamat dan hash disimpan lowercase dan dibatasi bentuknya oleh CHECK.
--   * Hak akses (GRANT/REVOKE) TIDAK ada di file ini; ada di 0002_roles.sql.
--   * Urutan pembuatan tabel penting: mandates -> active_mandates -> decisions -> ...
--   * File ini boleh diedit bebas sampai M0 di-commit. Sesudah itu terkunci; perubahan lewat 0002+.

-- ---------------------------------------------------------------------------
-- Fungsi bersama: append-only (dipakai audit_log dan mandates)
-- ---------------------------------------------------------------------------
CREATE FUNCTION append_only_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% on % is append-only (blocked)', TG_OP, TG_TABLE_NAME;
END;
$$;

-- ---------------------------------------------------------------------------
-- mandates: isi mandate ternormalisasi, dikunci oleh hash. Append-only.
-- ---------------------------------------------------------------------------
CREATE TABLE mandates (
  mandate_hash  TEXT PRIMARY KEY CHECK (mandate_hash ~ '^[0-9a-f]{64}$'),
  mandate_id    TEXT NOT NULL CHECK (char_length(mandate_id) BETWEEN 1 AND 64),
  agent_id      TEXT NOT NULL CHECK (agent_id ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
  content       JSONB NOT NULL,   -- mandate ternormalisasi; tanpa secret
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER mandates_no_change
  BEFORE UPDATE OR DELETE ON mandates
  FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE TRIGGER mandates_no_truncate
  BEFORE TRUNCATE ON mandates
  FOR EACH STATEMENT EXECUTE FUNCTION append_only_guard();

-- ---------------------------------------------------------------------------
-- active_mandates: mandate yang dimuat Guard terakhir, satu baris per agent.
-- Baru berubah saat Guard start. Guard mati != tidak ada mandate aktif.
-- ---------------------------------------------------------------------------
CREATE TABLE active_mandates (
  agent_id     TEXT PRIMARY KEY CHECK (agent_id ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
  mandate_hash TEXT NOT NULL REFERENCES mandates(mandate_hash),
  activated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- decisions: setiap keputusan Guard (termasuk hasil persetujuan manusia)
-- ---------------------------------------------------------------------------
CREATE TABLE decisions (
  id           UUID PRIMARY KEY,
  agent_id     TEXT NOT NULL CHECK (agent_id ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
  mandate_hash TEXT NOT NULL REFERENCES mandates(mandate_hash),
  chain_id     INT  NOT NULL CHECK (chain_id > 0),
  recipient    TEXT NOT NULL CHECK (recipient ~ '^0x[0-9a-f]{40}$'),
  amount_base  NUMERIC(78,0) NOT NULL CHECK (amount_base > 0),
  reason_text  TEXT CHECK (char_length(reason_text) <= 500),  -- data audit, bukan instruksi
  status       TEXT NOT NULL CHECK (status IN
                 ('approved','denied','needs_human','rejected_by_human','expired')),
  reason_code  TEXT NOT NULL CHECK (reason_code IN (
                 'approved','approved_by_human',
                 'retry_blocked','recipient_not_allowed',
                 'over_per_tx_limit','over_daily_limit',
                 'recheck_over_per_tx_limit','recheck_over_daily_limit',
                 'mandate_changed',
                 'human_approval_required',
                 'human_rejected',
                 'human_wait_timeout')),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at   TIMESTAMPTZ,   -- kosong hanya untuk needs_human
  approved_at  TIMESTAMPTZ,   -- hanya untuk approved
  valid_until  TIMESTAMPTZ,   -- hanya untuk approved
  expires_at   TIMESTAMPTZ,   -- snapshot batas human wait; diisi untuk needs_human dan turunannya
  envelope     JSONB,         -- hanya untuk approved (payload + key_id + signature)

  -- pasangan status <-> reason_code yang sah (daftar tertutup, sama dengan enum di kode)
  CONSTRAINT status_reason_pairs CHECK (
    (status = 'approved'          AND reason_code IN ('approved','approved_by_human'))
    OR (status = 'denied'         AND reason_code IN ('retry_blocked','recipient_not_allowed',
                                      'over_per_tx_limit','over_daily_limit',
                                      'recheck_over_per_tx_limit','recheck_over_daily_limit',
                                      'mandate_changed'))
    OR (status = 'needs_human'    AND reason_code = 'human_approval_required')
    OR (status = 'rejected_by_human' AND reason_code = 'human_rejected')
    OR (status = 'expired'        AND reason_code = 'human_wait_timeout')
  ),

  -- approved wajib lengkap; selain approved tidak boleh membawa data approval
  CONSTRAINT approved_is_complete CHECK (
    (status = 'approved' AND approved_at IS NOT NULL
                         AND valid_until IS NOT NULL
                         AND envelope    IS NOT NULL
                         AND decided_at  IS NOT NULL
                         AND approved_at = decided_at)
    OR
    (status <> 'approved' AND approved_at IS NULL
                          AND valid_until IS NULL
                          AND envelope    IS NULL)
  ),

  -- Opsi A: expires_at boleh terisi untuk turunan needs_human, dan dipakai sebagai snapshot
  CONSTRAINT decision_state_timestamps CHECK (
    (status = 'needs_human' AND decided_at IS NULL AND expires_at IS NOT NULL)
    OR (status = 'expired' AND expires_at IS NOT NULL AND decided_at = expires_at)
    OR (status NOT IN ('needs_human','expired') AND decided_at IS NOT NULL)
  ),

  CONSTRAINT valid_until_after_approval CHECK (
    valid_until IS NULL OR valid_until > approved_at
  ),
  CONSTRAINT expires_after_creation CHECK (
    expires_at IS NULL OR expires_at > created_at
  ),
  CONSTRAINT decided_not_before_created CHECK (
    decided_at IS NULL OR decided_at >= created_at
  ),
  -- keputusan tidak pernah dicatat setelah batas human wait
  CONSTRAINT decided_within_expiry CHECK (
    expires_at IS NULL OR decided_at IS NULL OR decided_at <= expires_at
  ),
  CONSTRAINT approved_within_expiry CHECK (
    expires_at IS NULL OR approved_at IS NULL OR approved_at <= expires_at
  )
);

-- cooldown (penolakan terakhir per agent + recipient)
CREATE INDEX decisions_cooldown_idx ON decisions (agent_id, recipient, decided_at);
-- jatah harian dan agregasi (hanya yang disetujui)
CREATE INDEX decisions_budget_idx   ON decisions (agent_id, approved_at)
  WHERE status = 'approved';
-- antrean persetujuan manusia, diurutkan berdasarkan batas waktu
CREATE INDEX decisions_pending_idx  ON decisions (expires_at)
  WHERE status = 'needs_human';

-- ---------------------------------------------------------------------------
-- executions: write-ahead Executor. UNIQUE(decision_id) = keputusan sekali pakai
-- ---------------------------------------------------------------------------
CREATE TABLE executions (
  id           BIGSERIAL PRIMARY KEY,
  decision_id  UUID NOT NULL UNIQUE REFERENCES decisions(id),
  tx_hash      TEXT NOT NULL UNIQUE CHECK (tx_hash ~ '^0x[0-9a-f]{64}$'),
  from_addr    TEXT NOT NULL CHECK (from_addr ~ '^0x[0-9a-f]{40}$'),
  to_addr      TEXT NOT NULL CHECK (to_addr   ~ '^0x[0-9a-f]{40}$'),
  amount_base  NUMERIC(78,0) NOT NULL CHECK (amount_base > 0),
  nonce        BIGINT NOT NULL CHECK (nonce >= 0),
  raw_tx       BYTEA  NOT NULL,   -- untuk rebroadcast idempoten saat recovery
  status       TEXT NOT NULL CHECK (status IN ('signed','broadcast','confirmed','failed')),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  broadcast_at TIMESTAMPTZ,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (from_addr, nonce),
  CONSTRAINT broadcast_has_timestamp CHECK (
    status = 'signed' OR status = 'failed' OR broadcast_at IS NOT NULL
  )
);

CREATE INDEX executions_status_idx ON executions (status);

-- ---------------------------------------------------------------------------
-- settlements: yang dilihat Watcher di chain. UNIQUE = exactly-once
-- ---------------------------------------------------------------------------
CREATE TABLE settlements (
  id               BIGSERIAL PRIMARY KEY,
  chain_id         INT    NOT NULL CHECK (chain_id > 0),
  tx_hash          TEXT   NOT NULL CHECK (tx_hash ~ '^0x[0-9a-f]{64}$'),
  log_index        INT    NOT NULL CHECK (log_index >= 0),
  block_number     BIGINT NOT NULL CHECK (block_number >= 0),
  block_hash       TEXT   NOT NULL CHECK (block_hash ~ '^0x[0-9a-f]{64}$'),
  from_addr        TEXT   NOT NULL CHECK (from_addr ~ '^0x[0-9a-f]{40}$'),
  to_addr          TEXT   NOT NULL CHECK (to_addr   ~ '^0x[0-9a-f]{40}$'),
  amount_base      NUMERIC(78,0) NOT NULL CHECK (amount_base >= 0),  -- uint256 boleh 0 di chain
  status           TEXT   NOT NULL CHECK (status IN ('pending','final','reorged')),
  first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  finalized_at     TIMESTAMPTZ,
  last_verified_at TIMESTAMPTZ,
  UNIQUE (chain_id, tx_hash, log_index),
  CONSTRAINT final_has_timestamp CHECK (
    (status = 'final' AND finalized_at IS NOT NULL) OR status <> 'final'
  )
);

CREATE INDEX settlements_status_block_idx ON settlements (status, block_number);

-- ---------------------------------------------------------------------------
-- reconciliations: satu baris per subjek, di-upsert; perubahan juga ke audit_log
-- ---------------------------------------------------------------------------
CREATE TABLE reconciliations (
  id            BIGSERIAL PRIMARY KEY,
  subject_type  TEXT NOT NULL CHECK (subject_type IN ('settlement','execution','decision')),
  subject_id    TEXT NOT NULL,
  state         TEXT NOT NULL CHECK (state IN
                  ('matched','unmatched_onchain','mismatch',
                   'executed_not_settled','expired','invalidated')),
  decision_id   UUID   REFERENCES decisions(id),
  execution_id  BIGINT REFERENCES executions(id),
  settlement_id BIGINT REFERENCES settlements(id),
  detail        JSONB,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (subject_type, subject_id)
);

CREATE INDEX reconciliations_state_idx ON reconciliations (state);

-- ---------------------------------------------------------------------------
-- watcher_state: posisi pemindaian terakhir per chain
-- ---------------------------------------------------------------------------
CREATE TABLE watcher_state (
  chain_id     INT PRIMARY KEY CHECK (chain_id > 0),
  last_scanned BIGINT NOT NULL CHECK (last_scanned >= 0)
);

-- ---------------------------------------------------------------------------
-- audit_log: append-only (UPDATE, DELETE, TRUNCATE ditolak trigger)
-- Nama event dijaga di kode (events.py); CHECK event di database menunggu M9.
-- ---------------------------------------------------------------------------
CREATE TABLE audit_log (
  id      BIGSERIAL PRIMARY KEY,
  ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor   TEXT  NOT NULL CHECK (actor IN ('guard','approver','executor','watcher','reconciler','human','migrator')),
  event   TEXT  NOT NULL CHECK (char_length(event) BETWEEN 1 AND 64),
  payload JSONB NOT NULL
);

CREATE INDEX audit_log_ts_idx ON audit_log (ts);
CREATE INDEX audit_log_event_idx ON audit_log (event, ts);

CREATE TRIGGER audit_log_no_change
  BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE TRIGGER audit_log_no_truncate
  BEFORE TRUNCATE ON audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION append_only_guard();

-- Catatan:
-- * Trigger tidak melindungi dari owner/superuser yang melepas trigger (RR-2).
--   Hak akses per proses ada di 0002_roles.sql.
-- * Advisory lock dipakai di kode aplikasi (lihat Arsitektur bagian 4), bukan di skema.
-- * Transisi status (mis. pending -> final, needs_human -> approved) ditegakkan di kode
--   dan tes, bukan di skema, kecuali constraint pasangan dan timestamp di atas.
