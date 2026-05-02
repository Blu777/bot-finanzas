from __future__ import annotations

from google.genai import types


DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-lite"


def low_latency_config(
    *,
    model: str,
    system_instruction: str,
    response_schema: dict,
    response_mime_type: str = "application/json",
    temperature: float = 0.0,
) -> types.GenerateContentConfig:
    kwargs = {
        "system_instruction": system_instruction,
        "response_mime_type": response_mime_type,
        "response_schema": response_schema,
        "temperature": temperature,
    }
    if _supports_thinking_level(model):
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_level="minimal")
    elif _supports_thinking_budget(model):
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    return types.GenerateContentConfig(**kwargs)


def _supports_thinking_level(model: str) -> bool:
    normalized = model.lower()
    return normalized.startswith("gemini-3") and "flash" in normalized


def _supports_thinking_budget(model: str) -> bool:
    return "gemini-2.5-flash" in model.lower()
