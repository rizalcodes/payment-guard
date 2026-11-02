-- Payment Guard — 0002_roles.sql
--
-- Hak minimum per proses. Sumber matriks: Arsitektur v1.1 bagian 3.3, dicerminkan
-- di src/payment_guard/privileges.py (dipakai tes ST-38, ST-39, ST-40).
--
-- ATURAN FILE:
--   * TANPA BEGIN/COMMIT. Runner migrasi yang memegang transaksi.
--   * HANYA hak tingkat tabel dan sequence. Tidak ada CREATE ROLE di sini, dan
--     tidak ada hak tingkat schema: keduanya wewenang `guard db bootstrap-roles`
--     yang dijalankan admin (Arsitektur v1.1 bagian 3.2).
--   * Hak diberikan ke ROLE GRUP (NOLOGIN). Role LOGIN per proses mewarisi
--     lewat keanggotaan grup, juga diatur bootstrap.
--   * Tidak ada DELETE, TRUNCATE, REFERENCES, TRIGGER, atau DDL untuk role
--     mana pun (Security v1.1 ST-34).
--   * schema_migrations tidak muncul di file ini dengan sengaja: hanya pemilik.

-- ---------------------------------------------------------------------------
-- Prasyarat: role grup harus sudah ada. Gagal keras, bukan diam-diam.
-- Security v1.1 ST-41.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  required CONSTANT text[] := ARRAY[
    'guard_rw', 'approver_rw', 'executor_rw', 'watcher_rw', 'reconciler_rw'
  ];
  missing text[];
BEGIN
  SELECT array_agg(candidate ORDER BY candidate) INTO missing
  FROM unnest(required) AS candidate
  WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = candidate);

  IF missing IS NOT NULL THEN
    RAISE EXCEPTION 'jalankan guard db bootstrap-roles dulu (role grup belum ada: %)',
      array_to_string(missing, ', ');
  END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- mandates — append-only, dijaga trigger. Tidak ada UPDATE untuk siapa pun.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT ON mandates TO guard_rw;
GRANT SELECT         ON mandates TO approver_rw;
GRANT SELECT         ON mandates TO executor_rw;
GRANT SELECT         ON mandates TO reconciler_rw;

-- ---------------------------------------------------------------------------
-- active_mandates — Guard menimpa barisnya saat start (UPSERT).
-- Matriks menulis "U" tanpa bintang: UPDATE tingkat tabel.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON active_mandates TO guard_rw;
GRANT SELECT                 ON active_mandates TO approver_rw;
GRANT SELECT                 ON active_mandates TO executor_rw;

-- ---------------------------------------------------------------------------
-- decisions — UPDATE per kolom sesuai transisi yang boleh dilakukan tiap proses.
--   guard    : hanya kedaluwarsa lazy (needs_human -> expired)
--   approver : approve, reject, dan kedaluwarsa lazy
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT ON decisions TO guard_rw;
GRANT UPDATE (status, reason_code, decided_at) ON decisions TO guard_rw;

GRANT SELECT ON decisions TO approver_rw;
GRANT UPDATE (status, reason_code, decided_at, approved_at, valid_until, envelope)
  ON decisions TO approver_rw;

GRANT SELECT ON decisions TO executor_rw;
GRANT SELECT ON decisions TO reconciler_rw;

-- ---------------------------------------------------------------------------
-- executions — write-ahead Executor. Daftar kolom dari Arsitektur v1.1 3.3.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT ON executions TO executor_rw;
GRANT UPDATE (status, broadcast_at, updated_at) ON executions TO executor_rw;

GRANT SELECT ON executions TO guard_rw;
GRANT SELECT ON executions TO approver_rw;
GRANT SELECT ON executions TO reconciler_rw;

-- ---------------------------------------------------------------------------
-- settlements — Watcher: pending -> final/reorged, dan perpindahan blok.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT ON settlements TO watcher_rw;
GRANT UPDATE (status, block_number, block_hash, finalized_at, last_verified_at)
  ON settlements TO watcher_rw;

GRANT SELECT ON settlements TO reconciler_rw;

-- ---------------------------------------------------------------------------
-- reconciliations — satu baris per subjek, di-upsert oleh Reconciler saja.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT ON reconciliations TO reconciler_rw;
GRANT UPDATE (state, detail, updated_at) ON reconciliations TO reconciler_rw;

-- ---------------------------------------------------------------------------
-- watcher_state — posisi pemindaian per chain. "U" tanpa bintang.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON watcher_state TO watcher_rw;

-- ---------------------------------------------------------------------------
-- audit_log — append-only. Semua proses bisa INSERT. Tidak ada UPDATE atau
-- DELETE untuk siapa pun; trigger menolaknya dan hak akses tidak memberinya.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT ON audit_log TO guard_rw;
GRANT SELECT, INSERT ON audit_log TO approver_rw;
GRANT SELECT, INSERT ON audit_log TO reconciler_rw;
GRANT         INSERT ON audit_log TO executor_rw;
GRANT         INSERT ON audit_log TO watcher_rw;

-- ---------------------------------------------------------------------------
-- Sequence — USAGE saja (untuk nextval lewat BIGSERIAL). Tanpa USAGE, INSERT
-- ke tabel BIGSERIAL ditolak meski INSERT pada tabelnya diberikan (ST-43).
-- Tanpa SELECT dan tanpa UPDATE: role aplikasi tidak boleh menggeser counter.
-- ---------------------------------------------------------------------------
GRANT USAGE ON SEQUENCE audit_log_id_seq TO guard_rw;
GRANT USAGE ON SEQUENCE audit_log_id_seq TO approver_rw;
GRANT USAGE ON SEQUENCE audit_log_id_seq TO executor_rw;
GRANT USAGE ON SEQUENCE audit_log_id_seq TO watcher_rw;
GRANT USAGE ON SEQUENCE audit_log_id_seq TO reconciler_rw;

GRANT USAGE ON SEQUENCE executions_id_seq      TO executor_rw;
GRANT USAGE ON SEQUENCE settlements_id_seq     TO watcher_rw;
GRANT USAGE ON SEQUENCE reconciliations_id_seq TO reconciler_rw;

-- Catatan:
-- * Trigger dan hak akses tidak melindungi dari pemilik objek atau superuser
--   (Security v1.1 RR-2). Pemilik objek = role migrator.
-- * Tidak ada GRANT ke PUBLIC di file ini. REVOKE ALL ON SCHEMA public FROM
--   PUBLIC dilakukan bootstrap, karena schema-level bukan wewenang migrator.
