-- Migration v3: Add amount_cents column for safe float->integer migration
-- Run this BEFORE deploying new code

-- 1. Add new column
ALTER TABLE ledger_entries ADD COLUMN amount_cents INTEGER;

-- 2. Backfill: convert float ARS/USD to cents (multiply by 100, round)
UPDATE ledger_entries 
SET amount_cents = CAST(ROUND(amount * 100) AS INTEGER)
WHERE amount_cents IS NULL;

-- 3. Add fingerprint columns for duplicate prevention
ALTER TABLE ledger_entries ADD COLUMN tx_fingerprint TEXT;
ALTER TABLE ledger_entries ADD COLUMN idempotency_key TEXT;

-- 4. Create fingerprints for existing rows
UPDATE ledger_entries
SET tx_fingerprint = 
    COALESCE(date, '') || '|' || 
    COALESCE(CAST(amount_cents AS TEXT), '0') || '|' ||
    LOWER(TRIM(COALESCE(description, '')));

-- 5. Create unique index on fingerprint (prevents duplicates)
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_fingerprint 
ON ledger_entries(tx_fingerprint);

-- 6. Create index on idempotency key for Firefly sync tracking
CREATE INDEX IF NOT EXISTS idx_ledger_idempotency 
ON ledger_entries(idempotency_key);

-- 7. Create table for idempotency tracking of external calls
CREATE TABLE IF NOT EXISTS idempotency_log (
    key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    external_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT  -- for cleanup
);

CREATE INDEX IF NOT EXISTS idx_idempotency_expires 
ON idempotency_log(expires_at);

-- Verify migration
SELECT 
    COUNT(*) as total_rows,
    COUNT(amount_cents) as with_cents,
    COUNT(tx_fingerprint) as with_fingerprint,
    COUNT(CASE WHEN amount_cents != CAST(ROUND(amount * 100) AS INTEGER) THEN 1 END) as mismatches
FROM ledger_entries;
