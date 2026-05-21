"""Safe money handling utilities - integer cents representation.

All financial amounts are stored and calculated as integer cents (1 = $0.01).
This avoids floating point precision issues entirely.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def parse_amount_to_cents(raw: str) -> int | None:
    """Parse user input (e.g., '15.5k', '1.250,50', '$100.50') to integer cents.
    
    Returns None if parsing fails.
    """
    if not raw or not raw.strip():
        return None
    
    s = raw.strip().lower()
    
    # Extract multiplier suffix
    multiplier = 1
    if s.endswith('k') or s.endswith('luca') or s.endswith('lucas'):
        multiplier = 1_000
        s = s.rstrip('kas').rstrip('luc')
    elif s.endswith('palo') or s.endswith('palos'):
        multiplier = 1_000_000
        s = s.rstrip('s').rstrip('palo')
    elif s.endswith('mil'):
        multiplier = 1_000
        s = s[:-3]
    
    # Remove currency symbols and whitespace
    s = re.sub(r'[\s$€£¥u\s]', '', s)
    
    # Handle comma vs dot decimal separators
    # Strategy: if both present, comma is usually decimal in AR locale
    if ',' in s and '.' in s:
        # 1.250,50 -> 1250.50 (comma is decimal)
        if s.rfind(',') > s.rfind('.'):
            s = s.replace('.', '').replace(',', '.')
        else:
            # 1,250.50 -> 1250.50 (dot is decimal)
            s = s.replace(',', '')
    elif ',' in s:
        # Could be decimal or thousand separator
        # If last comma has 2 digits after -> decimal
        parts = s.split(',')
        if len(parts[-1]) == 2 and parts[-1].isdigit():
            s = s.replace(',', '.')
        else:
            s = s.replace(',', '')
    
    try:
        decimal_val = Decimal(s)
        if decimal_val < 0:
            # Handle negative - multiply first, then negate
            cents = int((decimal_val * 100 * multiplier).to_integral_value(rounding=ROUND_HALF_UP))
        else:
            cents = int((decimal_val * 100 * multiplier).to_integral_value(rounding=ROUND_HALF_UP))
        return cents
    except (InvalidOperation, ValueError):
        return None


def cents_to_decimal(cents: int) -> Decimal:
    """Convert cents to Decimal for calculations."""
    return Decimal(cents) / Decimal(100)


def cents_to_display(cents: int, currency: str = "ARS") -> str:
    """Format cents for display: 150000 -> '$1,500.00'"""
    abs_cents = abs(cents)
    whole = abs_cents // 100
    frac = abs_cents % 100
    sign = '-' if cents < 0 else ''
    
    # Format with thousands separator
    whole_str = f"{whole:,}"
    result = f"{sign}{whole_str}.{frac:02d}"
    
    if currency == "ARS":
        return f"${result}"
    elif currency == "USD":
        return f"US${result}"
    return f"{currency} {result}"


def cents_to_float_for_display(cents: int) -> float:
    """Convert cents to float ONLY for display/math where precision loss is acceptable."""
    return cents / 100.0


def decimal_to_cents(d: Decimal) -> int:
    """Convert Decimal to cents with proper rounding."""
    return int((d * 100).to_integral_value(rounding=ROUND_HALF_UP))


def normalize_description(desc: str) -> str:
    """Create deterministic normalized description for fingerprinting."""
    import unicodedata
    # Lowercase, strip accents, collapse whitespace
    normalized = unicodedata.normalize('NFD', desc.lower())
    normalized = ''.join(c for c in normalized if unicodedata.category(c) != 'Mn')
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    # Remove filler words that don't distinguish transactions
    fillers = {'el', 'la', 'los', 'las', 'un', 'una', 'de', 'del', 'al', 'en', 'con', 'por', 'para'}
    words = [w for w in normalized.split() if w not in fillers]
    return ' '.join(words)


def generate_fingerprint(date_str: str, cents: int, description: str) -> str:
    """Generate unique transaction fingerprint for duplicate detection."""
    norm_desc = normalize_description(description)
    return f"{date_str}|{cents}|{norm_desc}"


def generate_idempotency_key(date_str: str, cents: int, description: str, 
                              account: str = "", tx_type: str = "") -> str:
    """Generate deterministic idempotency key for external API calls."""
    import hashlib
    fingerprint = generate_fingerprint(date_str, cents, description)
    data = f"{fingerprint}|{account}|{tx_type}"
    return hashlib.sha256(data.encode('utf-8')).hexdigest()[:32]
