from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nl_expense import parse_expense


def test_parse_expense_uses_requested_model():
    response = MagicMock()
    response.text = json.dumps(
        {
            "monto": 1000,
            "descripcion": "Cafe",
            "categoria": "Salidas",
            "cuenta": "",
            "tipo": "gasto",
            "fecha": "2026-05-02",
        }
    )

    with patch("nl_expense.genai") as mock_genai:
        mock_client = mock_genai.Client.return_value
        mock_client.models.generate_content.return_value = response

        parsed = parse_expense(
            "1000 cafe",
            gemini_api_key="fake-key",
            model="gemini-custom-test",
            categories=["Salidas"],
        )

    assert parsed.description == "Cafe"
    mock_client.models.generate_content.assert_called_once()
    assert mock_client.models.generate_content.call_args.kwargs["model"] == "gemini-custom-test"
