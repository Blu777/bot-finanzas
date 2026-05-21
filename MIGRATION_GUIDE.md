# Money Migration Guide: Float to Integer Cents

## Overview
This migration converts all financial amounts from `float` (imprecise) to `integer cents` (precise) representation.

**Why:** Floating point cannot exactly represent decimal values like 0.01, causing rounding errors in financial calculations.

**Migration Strategy:** 
- **Phase 1:** Schema migration (adds new columns, keeps old ones)
- **Phase 2:** Dual-write (writes to both columns, reads from new)
- **Phase 3:** External API idempotency
- **Phase 4:** Cleanup (future - remove float column after validation)

---

## Files Changed

### 1. NEW: `mp-sync/money_utils.py`
Core money handling utilities:
- `parse_amount_to_cents()` - Parse user input to cents
- `cents_to_display()` - Format for display
- `generate_fingerprint()` - Unique transaction identifier
- `generate_idempotency_key()` - For external API idempotency

### 2. MODIFIED: `mp-sync/nl_expense.py`
Major changes:
- `ParsedExpense.amount` → `ParsedExpense.amount_cents` (int)
- `LedgerRow.amount` → `LedgerRow.amount_cents` (int)
- `Ledger.append()` now uses `INSERT OR IGNORE` with fingerprint
- `Ledger.find_match()` uses exact integer comparison
- Database schema migration v3 (automatic on startup)

### 3. MODIFIED: `mp-sync/telegram_bot.py`
- Updated `_format_ledger_rows()` to use cents
- Updated `_format_parse_preview()` to use cents
- Updated validation logic to check `amount_cents == 0`
- Updated `cmd_deshacer()` display

### 4. MODIFIED: `mp-sync/firefly_import.py`
- `_post_tx()` now parses to cents internally
- `_parse_amount()` returns cents (int)

### 5. NEW: `mp-sync/migrations/v3_add_amount_cents.sql`
Manual migration script for production databases.

### 6. NEW: `mp-sync/test_money_migration.py`
Test suite to verify migration correctness.

---

## Migration Steps for Production

### Step 1: Backup Database
```bash
cp /data/ledger.sqlite /data/ledger.sqlite.backup.$(date +%Y%m%d)
```

### Step 2: Deploy Code
Deploy the new code. The `_apply_pending_migrations()` function will:
1. Add `amount_cents`, `tx_fingerprint`, `idempotency_key` columns
2. Backfill `amount_cents` from existing `amount` values
3. Create unique index on fingerprint

### Step 3: Verify Migration
Run the test script:
```bash
cd mp-sync
python test_money_migration.py
```

Check logs for:
```
Aplicando migracion DB v3: add amount_cents, fingerprint, idempotency_key
```

### Step 4: Validate Data
Check that amounts match:
```sql
-- In SQLite
SELECT 
    COUNT(*) as total,
    COUNT(amount_cents) as with_cents,
    COUNT(CASE WHEN amount_cents != CAST(ROUND(amount * 100) AS INTEGER) THEN 1 END) as mismatches
FROM ledger_entries;
```

Expected: `mismatches = 0`

---

## Key Design Decisions

### 1. Integer Cents (not Decimal)
- **Pros:** Fast, native SQLite support, no library dependency
- **Cons:** Limited to 2 decimal places (sufficient for most currencies)

### 2. Dual-Column Strategy
- Keep `amount` (float) as read-only legacy column
- Write to both `amount` and `amount_cents` during transition
- Read from `amount_cents` (with fallback to computed from `amount`)

### 3. Fingerprint-Based Duplicate Detection
- Fingerprint = `date|amount_cents|normalized_description`
- UNIQUE INDEX on fingerprint prevents duplicates at DB level
- Eliminates race condition in check-then-insert

### 4. Idempotency Keys
- SHA256 hash of fingerprint + account + tx_type
- Passed to Firefly as `external_id`
- Prevents duplicate external API calls on retry

---

## Backward Compatibility

### For Reading
```python
# LedgerRow still supports .amount property (converts on-the-fly)
row.amount_cents  # Primary: int (e.g., 15000)
row.amount        # Legacy: float (e.g., 150.0) - for display only
```

### For Writing
```python
# Always use amount_cents
LedgerRow(amount_cents=15000, ...)
ParsedExpense(amount_cents=15000, ...)

# Factory method for gradual migration
ParsedExpense.from_float(amount_float=150.0, ...)
```

---

## Testing Checklist

- [ ] `test_money_migration.py` passes
- [ ] Database migration applies without errors
- [ ] Existing transactions display correctly
- [ ] New transactions save with correct cents
- [ ] Duplicate detection prevents double entries
- [ ] Firefly sync works with idempotency keys
- [ ] CSV import handles amounts correctly
- [ ] Undo (deshacer) displays correct amounts
- [ ] Search results show correct amounts
- [ ] Stats report correct counts

---

## Rollback Plan

If issues occur:

1. **Stop the bot**
2. **Restore database from backup**
3. **Revert to previous code version**
4. **Verify data integrity**

The old code will continue to work because:
- `amount` column still exists and is populated
- No required columns were removed

---

## Post-Migration Cleanup (Future)

After 30 days of stable operation:

```sql
-- Remove legacy float column (optional)
ALTER TABLE ledger_entries DROP COLUMN amount;

-- Remove legacy indexes (optional)
DROP INDEX idx_ledger_amount_date;
```

**Note:** This is IRREVERSIBLE. Only do this after thorough validation.

---

## FAQ

**Q: Why not use Python's Decimal?**
A: SQLite doesn't have native Decimal support. Converting to/from strings adds complexity. Integer cents is simpler and sufficient for financial use cases with 2 decimal places.

**Q: Will this break existing data?**
A: No. The migration preserves all data. Old `amount` column is kept read-only.

**Q: What about currencies with 3 decimal places?**
A: Not supported by this migration. Would need to use "milli-cents" (multiply by 1000) or Decimal.

**Q: How do I verify no data corruption?**
A: Run the validation query above. Mismatches should be 0. If not, the float->int conversion needs investigation.
