"""Configuracion centralizada leida desde variables de entorno.

Todas las variables requeridas se validan al inicio; si alguna falta o es
invalida el bot falla rapido con un mensaje claro en lugar de crashear
mas tarde con un KeyError o ValueError críptico.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from nl_expense import parse_asset_account_map


@dataclass(frozen=True)
class Settings:
    bot_token: str
    allowed_chats: frozenset[int]
    firefly_url: str
    firefly_token: str
    asset_id: int
    asset_accounts: dict[str, int]
    currency: str
    rule_group_title: str
    gemini_api_key: str
    gemini_model: str
    local_ledger_csv: str

    @classmethod
    def from_env(cls) -> "Settings":
        errors: list[str] = []

        def require(name: str) -> str:
            val = os.environ.get(name, "").strip()
            if not val:
                errors.append(f"Requerida: {name}")
            return val

        def optional(name: str, default: str = "") -> str:
            return os.environ.get(name, default).strip()

        bot_token = require("TELEGRAM_BOT_TOKEN")
        firefly_url = require("FIREFLY_URL")
        firefly_token = require("FIREFLY_PERSONAL_TOKEN")

        raw_allowed = optional("TELEGRAM_ALLOWED_CHATS")
        allowed_chats: frozenset[int] = frozenset()
        if raw_allowed:
            try:
                allowed_chats = frozenset(
                    int(x) for x in raw_allowed.split(",") if x.strip()
                )
            except ValueError as e:
                errors.append(f"TELEGRAM_ALLOWED_CHATS contiene valores no numericos: {e}")

        raw_asset_id = optional("FIREFLY_ASSET_ACCOUNT_ID", "0")
        asset_id = 0
        try:
            asset_id = int(raw_asset_id)
            if asset_id <= 0:
                errors.append("FIREFLY_ASSET_ACCOUNT_ID debe ser un entero positivo")
        except ValueError:
            errors.append(
                f"FIREFLY_ASSET_ACCOUNT_ID no es un entero valido: {raw_asset_id!r}"
            )

        if errors:
            raise EnvironmentError(
                "Configuracion del bot invalida:\n"
                + "\n".join(f"  - {e}" for e in errors)
            )

        return cls(
            bot_token=bot_token,
            allowed_chats=allowed_chats,
            firefly_url=firefly_url,
            firefly_token=firefly_token,
            asset_id=asset_id,
            asset_accounts=parse_asset_account_map(
                optional("FIREFLY_ASSET_ACCOUNTS"),
                default_asset_id=asset_id,
            ),
            currency=optional("CURRENCY", "ARS"),
            rule_group_title=optional("RULE_GROUP_TITLE", "mp-bot"),
            gemini_api_key=optional("GEMINI_API_KEY"),
            gemini_model=optional("GEMINI_MODEL", "gemini-2.0-flash-lite"),
            local_ledger_csv=optional("LOCAL_LEDGER_CSV", "/data/ledger.csv"),
        )
