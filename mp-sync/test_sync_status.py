"""Quick test to verify sync_status is working correctly."""
import tempfile
import os
from nl_expense import Ledger, LedgerRow

# Redirect output to file
output = []

def log(msg):
    output.append(msg)
    print(msg)

# Create a temp database
db_path = "test_sync_debug.db"
if os.path.exists(db_path):
    os.remove(db_path)

# Create ledger
ledger = Ledger(db_path)

# Check if sync_status column exists
with ledger._db() as conn:
    cursor = conn.execute("PRAGMA table_info(ledger_entries)")
    columns = {row[1] for row in cursor.fetchall()}
    log(f"Columns in ledger_entries: {columns}")
    log(f"sync_status exists: {'sync_status' in columns}")

# Insert a test entry
row = LedgerRow(
    date="2024-01-15",
    description="Test transaction",
    amount_cents=1500,  # $15.00
    category="Test",
    account="Cash",
)
log(f"\nBefore append: sync_status = '{row.sync_status}'")

idx, was_inserted = ledger.append(row)
log(f"After append: idx={idx}, was_inserted={was_inserted}")

# Check what's in the database
with ledger._db() as conn:
    cursor = conn.execute("SELECT id, sync_status, firefly_id FROM ledger_entries WHERE id = ?", (idx,))
    db_row = cursor.fetchone()
    log(f"DB row after insert: {db_row}")

# Update sync_status
ledger.update_row(idx, sync_status="synced", firefly_id="test-123")
log(f"\nAfter update_row with sync_status='synced'")

# Check again
with ledger._db() as conn:
    cursor = conn.execute("SELECT id, sync_status, firefly_id FROM ledger_entries WHERE id = ?", (idx,))
    db_row = cursor.fetchone()
    log(f"DB row after update: {db_row}")

# Check stats
stats = ledger.stats()
log(f"\nStats: {stats}")

# Cleanup
if os.path.exists(db_path):
    os.remove(db_path)

log("\nTest completed!")

# Also write to file
with open("test_output.txt", "w") as f:
    f.write("\n".join(output))
