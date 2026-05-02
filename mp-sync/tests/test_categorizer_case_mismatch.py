"""Tests que demuestran el bug de case-mismatch en gemini_categorizer.

Bug: categorize_pending() usa una comparacion case-insensitive para verificar
si la categoria devuelta por Gemini existe en Firefly, pero luego aplica el
nombre con el casing ORIGINAL de Gemini en vez del casing canonico de Firefly.

Resultado: si Firefly tiene "Supermercado" y Gemini devuelve "supermercado",
el check pasa (ambos son iguales en lowercase), pero se actualiza la
transaccion con "supermercado" —creando una categoria duplicada en Firefly.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gemini_categorizer import categorize_pending, _build_prompt


# ---------------------------------------------------------------------------
# Helpers: Firefly mock
# ---------------------------------------------------------------------------

def _fake_firefly(categories: list[str], transactions: list[dict] | None = None):
    """Crea un FireflyClient mock con categorias y transacciones configurables."""
    client = MagicMock()

    client.list_categories.return_value = [
        {"id": str(i), "attributes": {"name": c}} for i, c in enumerate(categories)
    ]

    if transactions is None:
        transactions = [
            {
                "id": "100",
                "attributes": {
                    "transactions": [
                        {
                            "transaction_journal_id": 200,
                            "description": "Carrefour Express",
                            "date": "2025-04-20T00:00:00-03:00",
                            "amount": "-4500.00",
                            "category_name": "",
                            "tags": ["mercadopago"],
                        }
                    ]
                },
            }
        ]
    client.search_transactions.return_value = transactions

    client.update_transaction_category.return_value = {}

    client._get.return_value = {
        "data": {
            "attributes": {
                "transactions": [
                    {
                        "transaction_journal_id": 200,
                        "tags": ["mercadopago"],
                    }
                ]
            }
        }
    }
    client._request.return_value = MagicMock(status_code=200)
    client._h.return_value = {"Authorization": "Bearer x"}

    return client


def _fake_gemini_response(categories: list[str]):
    """Crea un response mock de Gemini que devuelve las categorias dadas."""
    resp = MagicMock()
    resp.text = json.dumps(categories)
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCategorizerCaseMismatch:
    """Reproduce el bug: Gemini devuelve el nombre de categoria con casing
    diferente al que existe en Firefly. El codigo actual acepta el match
    (case-insensitive) pero aplica el nombre con el casing INCORRECTO."""

    def test_gemini_returns_lowercase_category_applies_wrong_case(self):
        """BUG: Gemini devuelve 'supermercado' pero Firefly tiene 'Supermercado'.
        El check case-insensitive pasa, pero update_transaction_category recibe
        'supermercado' en vez de 'Supermercado'."""

        firefly = _fake_firefly(categories=["Supermercado", "Transporte", "Delivery"])
        gemini_response = _fake_gemini_response(["supermercado"])

        with patch("gemini_categorizer.genai") as mock_genai, \
             patch("gemini_categorizer.call_with_retries") as mock_retry:
            mock_retry.return_value = gemini_response

            result = categorize_pending(
                firefly,
                gemini_api_key="fake-key",
                tag_filter="mercadopago",
                model="gemini-2.5-flash",
            )

        assert result.classified == 1, (
            f"Deberia clasificar 1 tx, pero classified={result.classified}"
        )
        assert result.unknown == 0
        assert result.errors == 0

        firefly.update_transaction_category.assert_called_once()
        call_args = firefly.update_transaction_category.call_args
        applied_category = call_args[0][1]

        # ESTE ASSERT DEMUESTRA EL BUG:
        # El codigo actual pasa "supermercado" (casing de Gemini) en vez de
        # "Supermercado" (casing canonico de Firefly).
        assert applied_category == "Supermercado", (
            f"Se aplico '{applied_category}' en vez de 'Supermercado'. "
            f"Esto crea una categoria duplicada en Firefly con casing incorrecto."
        )

    def test_gemini_returns_allcaps_category_applies_wrong_case(self):
        """BUG variante: Gemini devuelve 'TRANSPORTE' pero Firefly tiene 'Transporte'."""

        firefly = _fake_firefly(categories=["Supermercado", "Transporte", "Delivery"])
        gemini_response = _fake_gemini_response(["TRANSPORTE"])

        with patch("gemini_categorizer.genai") as mock_genai, \
             patch("gemini_categorizer.call_with_retries") as mock_retry:
            mock_retry.return_value = gemini_response

            result = categorize_pending(
                firefly,
                gemini_api_key="fake-key",
                tag_filter="mercadopago",
                model="gemini-2.5-flash",
            )

        assert result.classified == 1

        call_args = firefly.update_transaction_category.call_args
        applied_category = call_args[0][1]

        assert applied_category == "Transporte", (
            f"Se aplico '{applied_category}' en vez de 'Transporte'."
        )

    def test_exact_case_match_works_correctly(self):
        """Control: cuando Gemini devuelve el casing exacto, todo funciona bien."""

        firefly = _fake_firefly(categories=["Supermercado", "Transporte", "Delivery"])
        gemini_response = _fake_gemini_response(["Supermercado"])

        with patch("gemini_categorizer.genai") as mock_genai, \
             patch("gemini_categorizer.call_with_retries") as mock_retry:
            mock_retry.return_value = gemini_response

            result = categorize_pending(
                firefly,
                gemini_api_key="fake-key",
                tag_filter="mercadopago",
                model="gemini-2.5-flash",
            )

        assert result.classified == 1
        assert result.errors == 0

        call_args = firefly.update_transaction_category.call_args
        applied_category = call_args[0][1]

        assert applied_category == "Supermercado"

    def test_unknown_category_marked_as_proposed_new(self):
        """Categoria nueva (no existe en Firefly) se marca como propuesta."""

        firefly = _fake_firefly(categories=["Supermercado"])
        gemini_response = _fake_gemini_response(["Kiosco"])

        with patch("gemini_categorizer.genai") as mock_genai, \
             patch("gemini_categorizer.call_with_retries") as mock_retry:
            mock_retry.return_value = gemini_response

            result = categorize_pending(
                firefly,
                gemini_api_key="fake-key",
                tag_filter="mercadopago",
                model="gemini-2.5-flash",
            )

        assert result.unknown == 1
        assert result.classified == 0
        assert "Kiosco" in result.proposed_new

    def test_empty_gemini_response_marked_as_unknown(self):
        """Gemini devuelve string vacio -> se marca como unknown."""

        firefly = _fake_firefly(categories=["Supermercado"])
        gemini_response = _fake_gemini_response([""])

        with patch("gemini_categorizer.genai") as mock_genai, \
             patch("gemini_categorizer.call_with_retries") as mock_retry:
            mock_retry.return_value = gemini_response

            result = categorize_pending(
                firefly,
                gemini_api_key="fake-key",
                tag_filter="mercadopago",
                model="gemini-2.5-flash",
            )

        assert result.unknown == 1
        assert result.classified == 0

    def test_uses_requested_model(self):
        firefly = _fake_firefly(categories=["Supermercado"])
        gemini_response = _fake_gemini_response(["Supermercado"])

        with patch("gemini_categorizer.genai") as mock_genai:
            mock_client = mock_genai.Client.return_value
            mock_client.models.generate_content.return_value = gemini_response

            categorize_pending(
                firefly,
                gemini_api_key="fake-key",
                model="gemini-custom-test",
            )

        mock_client.models.generate_content.assert_called_once()
        assert mock_client.models.generate_content.call_args.kwargs["model"] == "gemini-custom-test"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
