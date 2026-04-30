"""Tests para find_match con filtro por descripcion.

Bug: find_match solo comparaba monto + fecha (+-1 dia). Dos transferencias
distintas del mismo monto en el mismo dia (ej: Ivi +4000 y Joaco +4000)
se confundian, y la segunda se reportaba como "already_synced" de la primera.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nl_expense import Ledger, LedgerRow, _descriptions_compatible


# ---------------------------------------------------------------------------
# _descriptions_compatible unit tests
# ---------------------------------------------------------------------------


class TestDescriptionsCompatible:
    def test_same_strings(self):
        assert _descriptions_compatible("Transferencia Ivi", "Transferencia Ivi") is True

    def test_empty_a(self):
        assert _descriptions_compatible("", "Transferencia Ivi") is True

    def test_empty_b(self):
        assert _descriptions_compatible("Transferencia Ivi", "") is True

    def test_substring(self):
        assert _descriptions_compatible("uber", "uber eats") is True

    def test_different_names(self):
        assert _descriptions_compatible("Transferencia Ivi", "Transferencia Joaco") is False

    def test_different_names_case_insensitive(self):
        assert _descriptions_compatible("transferencia ivi", "TRANSFERENCIA JOACO") is False

    def test_accented_vs_unaccented(self):
        assert _descriptions_compatible("Devolución Joaco", "Devolucion Joaco") is True

    def test_superset_words(self):
        assert _descriptions_compatible("creditos mercado pago", "creditos de mercado pago") is True

    def test_different_gas_stations(self):
        assert _descriptions_compatible("Nafta Shell", "Nafta YPF") is False

    def test_same_single_word(self):
        assert _descriptions_compatible("Carrefour", "Carrefour") is True

    def test_different_months(self):
        assert _descriptions_compatible("Sueldo enero", "Sueldo febrero") is False

    # --- filler word tests (real MP CSV descriptions vs NL) ---

    def test_nl_vs_csv_transfer_same_person(self):
        """NL 'Transferencia Ivi' should match CSV 'Transferencia recibida Ivi'."""
        assert _descriptions_compatible(
            "Transferencia Ivi", "Transferencia recibida Ivi"
        ) is True

    def test_nl_short_vs_csv_full_name(self):
        """NL 'Transferencia Felipe' should match CSV with full name."""
        assert _descriptions_compatible(
            "Transferencia Felipe",
            "Transferencia recibida Felipe Faruk Berazain",
        ) is True

    def test_csv_enviada_vs_csv_recibida_same_person(self):
        """Same person, enviada vs recibida: 'transferencia' and 'enviada'/'recibida'
        are filler, so only significant word is the name -> compatible."""
        assert _descriptions_compatible(
            "Transferencia enviada Ainara",
            "Transferencia recibida Ainara",
        ) is True

    def test_csv_different_people_transfers(self):
        """Different people in CSV-style descriptions."""
        assert _descriptions_compatible(
            "Transferencia recibida Felipe Faruk Berazain",
            "Transferencia enviada Joaquin Venegas",
        ) is False

    def test_pago_same_store(self):
        """Same store, 'Pago' is filler."""
        assert _descriptions_compatible("Pago SUBE", "Pago SUBE Viajes") is True

    def test_pago_different_stores(self):
        assert _descriptions_compatible("Pago SUBE", "Pago Stop and Coffee") is False

    def test_filler_only_descriptions(self):
        """When all words are filler, can't discriminate -> compatible."""
        assert _descriptions_compatible("Transferencia enviada", "Transferencia recibida") is True

    def test_rendimientos(self):
        assert _descriptions_compatible("Rendimientos", "Rendimientos") is True

    def test_compra_vs_pago_same_store(self):
        """'Compra' and 'Pago' are both filler -> focuses on store name."""
        assert _descriptions_compatible("Compra Mercado Libre", "Pago Mercado Libre") is True

    def test_devolucion_vs_transferencia_same_person(self):
        """Both 'devolucion' and 'transferencia' are filler."""
        assert _descriptions_compatible(
            "Devolución transferencia Joaco",
            "Transferencia Joaco",
        ) is True


# ---------------------------------------------------------------------------
# Ledger.find_match integration tests
# ---------------------------------------------------------------------------


class TestFindMatchDescription:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.ledger = Ledger(str(tmp_path / "test.sqlite"))

    def _add(self, desc: str, amount: float, date: str, firefly_id: str = "") -> int:
        row = LedgerRow(
            date=date, description=desc, amount=amount, source="bot", firefly_id=firefly_id,
        )
        return self.ledger.append(row)

    def test_match_same_description(self):
        self._add("Transferencia Ivi", 4000, "2026-04-30", firefly_id="93")
        match = self.ledger.find_match(4000, "2026-04-30", description="Transferencia Ivi")
        assert match is not None
        assert match.description == "Transferencia Ivi"

    def test_no_match_different_description(self):
        self._add("Transferencia Ivi", 4000, "2026-04-30", firefly_id="93")
        match = self.ledger.find_match(4000, "2026-04-30", description="Transferencia Joaco")
        assert match is None

    def test_no_description_falls_back_to_amount_date(self):
        self._add("Transferencia Ivi", 4000, "2026-04-30", firefly_id="93")
        match = self.ledger.find_match(4000, "2026-04-30")
        assert match is not None
        assert match.description == "Transferencia Ivi"

    def test_two_entries_same_amount_different_people(self):
        self._add("Transferencia Ivi", 4000, "2026-04-30", firefly_id="93")
        self._add("Transferencia Joaco", 4000, "2026-04-30")
        match_joaco = self.ledger.find_match(4000, "2026-04-30", description="Transferencia Joaco")
        assert match_joaco is not None
        assert match_joaco.description == "Transferencia Joaco"
        match_ivi = self.ledger.find_match(4000, "2026-04-30", description="Transferencia Ivi")
        assert match_ivi is not None
        assert match_ivi.description == "Transferencia Ivi"

    def test_different_amount_no_match(self):
        self._add("Transferencia Ivi", 4000, "2026-04-30")
        match = self.ledger.find_match(5000, "2026-04-30", description="Transferencia Ivi")
        assert match is None

    def test_tolerance_days(self):
        self._add("Transferencia Ivi", 4000, "2026-04-29")
        match = self.ledger.find_match(4000, "2026-04-30", description="Transferencia Ivi")
        assert match is not None

    def test_outside_tolerance(self):
        self._add("Transferencia Ivi", 4000, "2026-04-27")
        match = self.ledger.find_match(4000, "2026-04-30", description="Transferencia Ivi")
        assert match is None

    def test_nl_matches_csv_style_description(self):
        """NL 'Transferencia Felipe' matches ledger 'Transferencia recibida Felipe Faruk'."""
        self._add("Transferencia recibida Felipe Faruk Berazain", 30000, "2026-03-21", firefly_id="50")
        match = self.ledger.find_match(30000, "2026-03-21", description="Transferencia Felipe")
        assert match is not None
        assert "Felipe" in match.description
