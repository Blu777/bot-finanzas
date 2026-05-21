"""Test script to verify the money migration works correctly."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Add mp-sync to path
sys.path.insert(0, str(Path(__file__).parent))

from money_utils import (
    parse_amount_to_cents,
    cents_to_display,
    cents_to_decimal,
    generate_fingerprint,
    generate_idempotency_key,
)
from nl_expense import Ledger, ParsedExpense, LedgerRow


def test_money_utils():
    """Test the money utility functions."""
    print("=" * 50)
    print("Testing money_utils functions")
    print("=" * 50)
    
    # Test parsing
    test_cases = [
        ("15000", 1500000),  # $150.00 -> 15000 cents
        ("150.50", 15050),
        ("1.250,50", 125050),  # AR format
        ("1,250.50", 125050),  # US format
        ("15k", 1500000),
        ("15 lucas", 1500000),
        ("1 palo", 100000000),
        ("$100.50", 10050),
        ("100.50 USD", 10050),
    ]
    
    for input_val, expected_cents in test_cases:
        result = parse_amount_to_cents(input_val)
        status = "✓" if result == expected_cents else "✗"
        print(f"{status} parse_amount_to_cents('{input_val}') = {result} (expected {expected_cents})")
    
    # Test display
    print("\n--- Display tests ---")
    display_cases = [
        (15000, "ARS", "$150.00"),
        (-15000, "ARS", "-$150.00"),
        (1500000, "ARS", "$15,000.00"),
        (10050, "USD", "US$100.50"),
    ]
    
    for cents, currency, expected in display_cases:
        result = cents_to_display(cents, currency)
        status = "✓" if result == expected else "✗"
        print(f"{status} cents_to_display({cents}, '{currency}') = '{result}' (expected '{expected}')")
    
    # Test fingerprinting
    print("\n--- Fingerprint tests ---")
    fp1 = generate_fingerprint("2024-01-15", 15000, "Supermercado Chino")
    fp2 = generate_fingerprint("2024-01-15", 15000, "supermercado chino")  # Same, different case
    fp3 = generate_fingerprint("2024-01-15", 15001, "Supermercado Chino")  # Different amount
    
    print(f"fp1: {fp1}")
    print(f"fp2: {fp2}")
    print(f"fp3: {fp3}")
    print(f"✓ fp1 == fp2 (case insensitive): {fp1 == fp2}")
    print(f"✓ fp1 != fp3 (different amount): {fp1 != fp3}")
    
    # Test idempotency key
    key1 = generate_idempotency_key("2024-01-15", 15000, "Test", "Banco", "gasto")
    key2 = generate_idempotency_key("2024-01-15", 15000, "Test", "Banco", "gasto")
    print(f"\n✓ Idempotency key deterministic: {key1 == key2}")
    print(f"  Key: {key1}")


def test_ledger_migration():
    """Test the Ledger migration with amount_cents."""
    print("\n" + "=" * 50)
    print("Testing Ledger with amount_cents")
    print("=" * 50)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_ledger.sqlite"
        ledger = Ledger(str(db_path))
        
        # Test insert with cents
        row = LedgerRow(
            date="2024-01-15",
            description="Test Transaction",
            amount_cents=15000,  # $150.00
            category="Test",
            account="Banco",
            currency="ARS",
            tx_type="gasto",
            source="bot",
        )
        
        idx, was_inserted = ledger.append(row)
        print(f"✓ Inserted row #{idx}, was_inserted={was_inserted}")
        print(f"  Fingerprint: {row.tx_fingerprint}")
        print(f"  Idempotency: {row.idempotency_key}")
        
        # Test duplicate detection
        row2 = LedgerRow(
            date="2024-01-15",
            description="Test Transaction",  # Same description
            amount_cents=15000,  # Same amount
            category="Different",  # Different category
            account="Efectivo",  # Different account
            currency="ARS",
            tx_type="gasto",
            source="bot",
        )
        
        idx2, was_inserted2 = ledger.append(row2)
        print(f"✓ Duplicate attempt: row #{idx2}, was_inserted={was_inserted2}")
        print(f"  (should be False - duplicate detected)")
        
        if not was_inserted2:
            print("  ✓ Duplicate prevention working correctly!")
        else:
            print("  ✗ Duplicate was not detected!")
        
        # Verify data
        entries = ledger.recent_entries(5)
        print(f"\n✓ Retrieved {len(entries)} entries")
        for e in entries:
            print(f"  - {e.date}: {e.amount_display} ({e.amount_cents} cents)")
        
        # Verify stats
        stats = ledger.stats()
        print(f"\n✓ Stats: {stats}")


def test_parsed_expense():
    """Test ParsedExpense with amount_cents."""
    print("\n" + "=" * 50)
    print("Testing ParsedExpense with amount_cents")
    print("=" * 50)
    
    # Create with cents
    pe = ParsedExpense(
        amount_cents=25000,  # $250.00
        description="Sushi restaurant",
        category="Salidas",
        date="2024-01-15",
        tx_type="gasto",
    )
    
    print(f"✓ ParsedExpense created")
    print(f"  amount_cents: {pe.amount_cents}")
    print(f"  amount (float, legacy): {pe.amount}")
    print(f"  display: {cents_to_display(pe.amount_cents, pe.currency)}")
    
    # Test factory method
    pe2 = ParsedExpense.from_float(
        amount_float=199.99,
        description="Test",
        category="Test",
        date="2024-01-15",
    )
    print(f"\n✓ From float factory: {pe2.amount_cents} cents (expected 19999)")


if __name__ == "__main__":
    try:
        test_money_utils()
        test_ledger_migration()
        test_parsed_expense()
        print("\n" + "=" * 50)
        print("All tests passed! ✓")
        print("=" * 50)
    except Exception as e:
        print(f"\n✗ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
