from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from firefly_client import FireflyClient, FireflyError


def test_transaction_exists_raises_when_search_fails():
    client = FireflyClient("https://firefly.example", "token")
    response = MagicMock(status_code=503, text="temporarily unavailable")
    client._request = MagicMock(return_value=response)

    with pytest.raises(FireflyError, match="external_id:mp-123"):
        client.transaction_exists("mp-123")
