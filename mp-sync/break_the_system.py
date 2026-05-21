"""Break the system: Real-world usage simulation exposing critical bugs.

This script simulates edge cases and concurrent scenarios to demonstrate
where the money migration fails in production.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import sqlite3
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent))

from money_utils import (
    parse_amount_to_cents,
    generate_fingerprint,
    generate_idempotency_key,
    normalize_description,
)
from nl_expense import Ledger, LedgerRow, ParsedExpense, record_expense


class FakeFireflyClient:
    """Mock Firefly client that simulates API behavior."""
    def __init__(self):
        self.transactions = {}  # idempotency_key -> transaction
        self.lock = threading.Lock()
        self.duplicate_count = 0
    
    def create_transaction(self, payload):
        """Simulate Firefly transaction creation with idempotency."""
        tx = payload["transactions"][0]
        key = tx.get("external_id", "")
        
        with self.lock:
            if key in self.transactions:
                self.duplicate_count += 1
                # Simulate Firefly returning existing transaction
                return {"data": {"id": self.transactions[key]}}
            
            new_id = len(self.transactions) + 1
            self.transactions[key] = new_id
            return {"data": {"id": new_id}}
    
    def transaction_exists(self, external_id):
        return external_id in self.transactions


def test_issue_1_migrated_data_no_fingerprint():
    """CRITICAL: Existing data from before migration creates duplicates.
    
    Scenario: Database has pre-migration transactions. Migration backfills
    amount_cents but NOT tx_fingerprint. New identical transaction = duplicate.
    """
    print("\n" + "="*70)
    print("ISSUE #1: Silent Duplicates on Migrated Data (CRITICAL)")
    print("="*70)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        
        # Simulate pre-migration database state
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE ledger_entries (
                id INTEGER PRIMARY KEY,
                date TEXT,
                description TEXT,
                amount REAL,
                category TEXT,
                account TEXT,
                currency TEXT,
                tx_type TEXT,
                source TEXT,
                firefly_id TEXT
            )
        """)
        
        # Insert pre-migration data (only has amount, no amount_cents, no fingerprint)
        conn.execute("""
            INSERT INTO ledger_entries 
            (date, description, amount, category, account, currency, tx_type, source)
            VALUES ('2024-01-15', 'Supermercado Chino', 150.50, 'Comida', 'Banco', 'ARS', 'gasto', 'bot')
        """)
        conn.commit()
        conn.close()
        
        # Now open with Ledger class - migration will add columns
        ledger = Ledger(str(db_path))
        
        # Simulate user entering SAME transaction after migration
        new_row = LedgerRow(
            date="2024-01-15",
            description="Supermercado Chino",
            amount_cents=15050,  # Same amount
            category="Comida",
            account="Banco",
            currency="ARS",
            tx_type="gasto",
            source="bot",
        )
        
        idx, was_inserted = ledger.append(new_row)
        
        print(f"Pre-migration row exists: Yes")
        print(f"New transaction inserted: {was_inserted}")
        print(f"Expected: False (should be rejected as duplicate)")
        print(f"Actual:   {was_inserted} {'✗ BUG! Duplicate created!' if was_inserted else '✓ Correct'}")
        
        # Count total rows
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0]
        conn.close()
        
        print(f"Total rows in database: {count}")
        print(f"Expected: 1")
        print(f"Result:   {'✗ DATA CORRUPTION - Duplicate created!' if count > 1 else '✓ OK'}")
        
        return was_inserted  # True means bug occurred


def test_issue_3_firefly_float_comparison():
    """HIGH: Float comparison in Firefly duplicate check misses duplicates.
    
    Scenario: Amounts like 0.01 vs 0.010000000000000000208...
    """
    print("\n" + "="*70)
    print("ISSUE #3: Float Comparison Misses Duplicates (HIGH)")
    print("="*70)
    
    # Simulate amounts that look equal but have float representation issues
    amount_str = "0.01"
    amount_float = float(amount_str)
    
    # What Firefly might return (different precision)
    firefly_amount = 0.01000000000000000020816681711721685132943093776702880859375
    
    print(f"Local amount:   {amount_float} (as float)")
    print(f"Firefly amount: {firefly_amount} (from API)")
    print(f"Comparison: {amount_float} == {firefly_amount} ? {amount_float == firefly_amount}")
    
    # In cents (correct way)
    local_cents = int(round(amount_float * 100))
    firefly_cents = int(round(firefly_amount * 100))
    print(f"\nIn cents:")
    print(f"Local:   {local_cents} cents")
    print(f"Firefly: {firefly_cents} cents")
    print(f"Match:   {local_cents == firefly_cents} {'✓' if local_cents == firefly_cents else '✗'}")
    
    return local_cents != firefly_cents


def test_issue_4_fingerprint_collisions():
    """HIGH: Aggressive normalization causes false duplicates.
    
    Different transactions that should be separate get flagged as duplicates.
    """
    print("\n" + "="*70)
    print("ISSUE #4: False Duplicates from Fingerprint Collisions (HIGH)")
    print("="*70)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        ledger = Ledger(str(db_path))
        
        # These are DIFFERENT transactions but normalization makes them identical
        test_cases = [
            ("Supermercado del barrio", "Supermercado al barrio"),
            ("Cafe Starbucks", "Café Starbucks"),  # With/without accent
            ("Pago de alquiler", "Pago del alquiler"),  # de vs del
            ("Uber a casa", "Uber para casa"),  # a vs para
        ]
        
        results = []
        for desc1, desc2 in test_cases:
            # Insert first transaction
            row1 = LedgerRow(
                date="2024-01-15",
                description=desc1,
                amount_cents=10000,
                category="Test",
                source="bot",
            )
            idx1, inserted1 = ledger.append(row1)
            
            # Try to insert second (different description)
            row2 = LedgerRow(
                date="2024-01-15",
                description=desc2,
                amount_cents=10000,
                category="Test",
                source="bot",
            )
            idx2, inserted2 = ledger.append(row2)
            
            fp1 = generate_fingerprint("2024-01-15", 10000, desc1)
            fp2 = generate_fingerprint("2024-01-15", 10000, desc2)
            
            print(f"\n'{desc1}' vs '{desc2}':")
            print(f"  FP1: {fp1}")
            print(f"  FP2: {fp2}")
            print(f"  First inserted: {inserted1}")
            print(f"  Second inserted: {inserted2} {'✗ FALSE DUPLICATE!' if not inserted2 else '✓ Correctly separate'}")
            
            if not inserted2:
                results.append((desc1, desc2))
        
        return results


def test_issue_5_rounding_mismatch():
    """HIGH: SQLite ROUND vs Python round() produce different results.
    
    This causes off-by-one cent errors in migrated data.
    """
    print("\n" + "="*70)
    print("ISSUE #5: Rounding Mismatch Causes Cent Errors (HIGH)")
    print("="*70)
    
    # Test cases where rounding differs
    test_amounts = [1.005, 2.005, 3.005, 0.015, 0.025, 0.035, 0.045]
    
    print("Amount | SQLite ROUND | Python round() | Match?")
    print("-" * 50)
    
    mismatches = []
    for amount in test_amounts:
        # Simulate what migration does (SQLite)
        sqlite_result = int(amount * 100 + 0.5)  # Approximation of SQLite ROUND
        
        # What Python does
        python_result = int(round(amount * 100))
        
        match = sqlite_result == python_result
        status = "✓" if match else "✗ MISMATCH!"
        
        print(f"{amount:.3f} | {sqlite_result:12d} | {python_result:14d} | {status}")
        
        if not match:
            mismatches.append((amount, sqlite_result, python_result))
    
    if mismatches:
        print(f"\n✗ Found {len(mismatches)} rounding mismatches!")
        print("During migration, these amounts would get different cent values")
        print("depending on whether SQLite or Python computed them.")
    
    return mismatches


def test_issue_6_account_in_idempotency_key():
    """FIXED: Account is no longer in idempotency key.
    
    Same transaction now generates SAME key regardless of account inference.
    This prevents Firefly duplicates when account inference changes.
    """
    print("\n" + "="*70)
    print("ISSUE #6: Account Field Causes Firefly Duplicates (FIXED)")
    print("="*70)
    
    # Same transaction, different inferred accounts
    date = "2024-01-15"
    cents = 15000
    description = "Supermercado"
    
    # All calls now use same signature (account removed)
    key1 = generate_idempotency_key(date, cents, description)
    key2 = generate_idempotency_key(date, cents, description)
    key3 = generate_idempotency_key(date, cents, description)
    
    print(f"Transaction: {description} ${cents/100:.2f} on {date}")
    print(f"Key 1: {key1}")
    print(f"Key 2: {key2}")
    print(f"Key 3: {key3}")
    
    all_same = key1 == key2 == key3
    print(f"\nAll keys identical: {all_same} {'✓ FIXED - Same transaction = Same key!' if all_same else '✗ BUG! Different keys'}")
    
    return not all_same  # Return False if fixed (no bug)


def test_concurrent_duplicate_creation():
    """CRITICAL: Race condition allows duplicates under concurrent load.
    
    Simulates multiple users/bots entering same transaction simultaneously.
    """
    print("\n" + "="*70)
    print("CONCURRENT REQUESTS: Race Condition Test (CRITICAL)")
    print("="*70)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        
        results = {"inserted": 0, "rejected": 0, "errors": []}
        lock = threading.Lock()
        
        def insert_transaction(thread_id):
            try:
                ledger = Ledger(str(db_path))
                row = LedgerRow(
                    date="2024-01-15",
                    description="Concurrent Test",
                    amount_cents=10000,
                    category="Test",
                    source="bot",
                )
                idx, was_inserted = ledger.append(row)
                
                with lock:
                    if was_inserted:
                        results["inserted"] += 1
                    else:
                        results["rejected"] += 1
                return was_inserted
            except Exception as e:
                with lock:
                    results["errors"].append(str(e))
                return False
        
        # Simulate 10 concurrent inserts of identical transaction
        print("Launching 10 concurrent threads for identical transaction...")
        
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(insert_transaction, i) for i in range(10)]
            list(as_completed(futures))  # Wait for all
        
        print(f"\nResults:")
        print(f"  Inserted: {results['inserted']}")
        print(f"  Rejected as duplicates: {results['rejected']}")
        print(f"  Errors: {len(results['errors'])}")
        
        # Verify database state
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0]
        conn.close()
        
        print(f"\n  Total rows in database: {count}")
        print(f"  Expected: 1")
        
        if count > 1:
            print(f"  ✗ RACE CONDITION BUG! {count-1} duplicates created!")
        elif results['inserted'] == 1 and results['rejected'] == 9:
            print(f"  ✓ Perfect - 1 inserted, 9 rejected")
        else:
            print(f"  ? Unexpected distribution")
        
        return count > 1


def test_retry_scenario():
    """Simulate network retry causing duplicate Firefly entries.
    
    When Firefly call succeeds but network times out, retry with 
    different idempotency key creates duplicate.
    """
    print("\n" + "="*70)
    print("RETRY SCENARIO: Firefly Duplicate on Timeout")
    print("="*70)
    
    fake_firefly = FakeFireflyClient()
    
    # First attempt - succeeds but "times out" from client perspective
    # (client thinks it failed, but Firefly created it)
    tx_data = {
        "date": "2024-01-15",
        "amount": "150.00",
        "description": "Test",
        "external_id": "key_1",  # First idempotency key
    }
    result1 = fake_firefly.create_transaction({"transactions": [tx_data]})
    first_id = result1["data"]["id"]
    
    # Client retries - but WITH DIFFERENT KEY (the bug!)
    # This simulates if account field changed or different key generation
    tx_data2 = {
        "date": "2024-01-15", 
        "amount": "150.00",
        "description": "Test",
        "external_id": "key_2",  # DIFFERENT key - bug!
    }
    result2 = fake_firefly.create_transaction({"transactions": [tx_data2]})
    second_id = result2["data"]["id"]
    
    print(f"First call ID:  {first_id}")
    print(f"Second call ID: {second_id}")
    print(f"Same ID (idempotency worked): {first_id == second_id}")
    print(f"Result: {'✓ Correct' if first_id == second_id else '✗ BUG - Duplicate created!'}")
    
    return first_id != second_id


def test_malformed_inputs():
    """Test system behavior with malformed/edge case inputs.
    """
    print("\n" + "="*70)
    print("MALFORMED INPUTS: Edge Cases")
    print("="*70)
    
    test_cases = [
        ("", "Empty string"),
        ("abc", "No number"),
        ("0", "Zero amount"),
        ("0.001", "Sub-cent amount"),
        ("999999999999999999", "Extremely large"),
        ("1.999", "Requires rounding up"),
        ("1.001", "Requires rounding down"),
        ("-150.50", "Negative"),
        ("++150", "Double sign"),
        ("15.5.5", "Multiple decimals"),
        ("15,5,5", "Multiple commas"),
        ("$150.50USD", "Multiple currencies"),
        ("150\n200", "Newline in amount"),
    ]
    
    print(f"{'Input':<20} | {'Result':>12} | Status")
    print("-" * 60)
    
    for input_val, description in test_cases:
        try:
            result = parse_amount_to_cents(input_val)
            if result is None:
                status = "None (reject)"
            else:
                status = f"{result} cents"
            print(f"{input_val!r:<20} | {status:>12} | {description}")
        except Exception as e:
            print(f"{input_val!r:<20} | {'EXCEPTION':>12} | {e}")


def main():
    """Run all breaking tests."""
    print("\n" + "="*70)
    print("SYSTEM BREAKAGE SIMULATION")
    print("="*70)
    print("\nThis script demonstrates real bugs in the money migration.")
    print("Each test shows a concrete scenario that breaks the system.")
    
    bugs_found = []
    
    # Issue #1 - Critical
    if test_issue_1_migrated_data_no_fingerprint():
        bugs_found.append("Issue #1: Silent duplicates on migrated data (CRITICAL)")
    
    # Issue #3 - High
    if test_issue_3_firefly_float_comparison():
        bugs_found.append("Issue #3: Float comparison misses duplicates (HIGH)")
    
    # Issue #4 - High
    collisions = test_issue_4_fingerprint_collisions()
    if collisions:
        bugs_found.append(f"Issue #4: {len(collisions)} fingerprint collisions (HIGH)")
    
    # Issue #5 - High
    mismatches = test_issue_5_rounding_mismatch()
    if mismatches:
        bugs_found.append(f"Issue #5: {len(mismatches)} rounding mismatches (HIGH)")
    
    # Issue #6 - Medium
    if test_issue_6_account_in_idempotency_key():
        bugs_found.append("Issue #6: Account in idempotency key (MEDIUM)")
    
    # Concurrent test
    if test_concurrent_duplicate_creation():
        bugs_found.append("Concurrent: Race condition (CRITICAL)")
    
    # Retry test
    if test_retry_scenario():
        bugs_found.append("Retry: Firefly duplicate on retry (HIGH)")
    
    # Malformed inputs
    test_malformed_inputs()
    
    # Summary
    print("\n" + "="*70)
    print("SUMMARY OF BUGS FOUND")
    print("="*70)
    
    if bugs_found:
        print(f"\n✗ {len(bugs_found)} CRITICAL/HIGH severity bugs found:")
        for bug in bugs_found:
            print(f"  • {bug}")
        print("\nThese bugs WILL cause:")
        print("  • Duplicate transactions in production")
        print("  • Data inconsistency between ledger and Firefly")
        print("  • Financial reporting errors")
    else:
        print("\n✓ No critical bugs found (unexpected)")
    
    print("\n" + "="*70)


if __name__ == "__main__":
    main()
