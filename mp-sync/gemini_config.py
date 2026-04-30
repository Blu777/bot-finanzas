from __future__ import annotations

from google.genai import types


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
    if _supports_thinking_budget(model):
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    return types.GenerateContentConfig(**kwargs)


def _supports_thinking_budget(model: str) -> bool:
    return "gemini-2.5-flash" in model.lower()
