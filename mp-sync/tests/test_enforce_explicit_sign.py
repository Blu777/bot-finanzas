"""Tests para _enforce_explicit_sign.

Bug: el LLM ignora el signo explicito del usuario. 'devolucion transferencia
Joaco -4000' se parseaba como +4000, matcheando contra la transferencia
entrante de Joaco ya registrada.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nl_expense import _enforce_explicit_sign


class TestEnforceExplicitSign:
    def test_minus_overrides_positive(self):
        assert _enforce_explicit_sign("devolucion Joaco -4000", 4000.0) == -4000.0

    def test_plus_overrides_negative(self):
        assert _enforce_explicit_sign("transferencia Ivi +4000", -4000.0) == 4000.0

    def test_minus_keeps_negative(self):
        assert _enforce_explicit_sign("gasto -500", -500.0) == -500.0

    def test_plus_keeps_positive(self):
        assert _enforce_explicit_sign("sueldo +50000", 50000.0) == 50000.0

    def test_no_sign_no_override(self):
        assert _enforce_explicit_sign("7000 chino", -7000.0) == -7000.0

    def test_no_sign_positive_stays(self):
        assert _enforce_explicit_sign("transferencia 4000", 4000.0) == 4000.0

    def test_zero_amount_unchanged(self):
        assert _enforce_explicit_sign("-4000", 0.0) == 0.0

    def test_minus_with_k(self):
        assert _enforce_explicit_sign("devolucion -4k", 4000.0) == -4000.0

    def test_plus_with_lucas(self):
        assert _enforce_explicit_sign("+15 lucas sueldo", -15000.0) == 15000.0

    def test_minus_with_spaces(self):
        assert _enforce_explicit_sign("devolucion - 4000 pesos", 4000.0) == -4000.0

    def test_minus_at_start(self):
        assert _enforce_explicit_sign("-4000 devolucion Joaco", 4000.0) == -4000.0

    def test_embedded_minus_in_word_ignored(self):
        """A hyphen inside a word (e.g. 'coca-cola') should not be treated as a sign."""
        assert _enforce_explicit_sign("coca-cola 500", -500.0) == -500.0

    def test_real_devolucion_message(self):
        """Exact message from the user's bug report."""
        assert _enforce_explicit_sign(
            "devolución transferencia Joaco -4000", 4000.0
        ) == -4000.0
