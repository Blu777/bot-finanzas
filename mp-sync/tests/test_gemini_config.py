from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gemini_config import low_latency_config


def test_low_latency_config_minimizes_31_flash_lite_thinking():
    config = low_latency_config(
        model="gemini-3.1-flash-lite-preview",
        system_instruction="system",
        response_schema={"type": "OBJECT"},
    )

    assert config.thinking_config is not None
    assert config.thinking_config.thinking_level.value == "MINIMAL"
    assert config.system_instruction == "system"
    assert config.response_mime_type == "application/json"
    assert config.response_schema == {"type": "OBJECT"}
    assert config.temperature == 0.0


def test_low_latency_config_disables_25_thinking():
    config = low_latency_config(
        model="gemini-2.5-flash-lite",
        system_instruction="system",
        response_schema={"type": "OBJECT"},
    )

    assert config.thinking_config is not None
    assert config.thinking_config.thinking_budget == 0
    assert config.system_instruction == "system"
    assert config.response_mime_type == "application/json"
    assert config.response_schema == {"type": "OBJECT"}
    assert config.temperature == 0.0


def test_low_latency_config_skips_unsupported_thinking_budget():
    config = low_latency_config(
        model="gemini-1.5-flash",
        system_instruction="system",
        response_schema={"type": "ARRAY"},
    )

    assert config.thinking_config is None
