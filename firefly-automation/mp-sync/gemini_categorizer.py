"""Categorizador de transacciones usando Google Gemini."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from google import genai
from google.genai import types

from firefly_client import FireflyClient


log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Sos un clasificador de transacciones financieras en español (Argentina).
Recibis una lista de transacciones y una lista de categorias existentes.
Para cada transaccion devolves la categoria que mejor la describa, eligiendo
SOLO de la lista de categorias provista. Si ninguna categoria encaja con
seguridad razonable, usa "UNKNOWN".

Reglas:
- Compras en supermercados (Carrefour, Coto, Disco, Jumbo, Vea, Dia, Walmart, Chango Mas) -> "Supermercado".
- Pagos SUBE, combustibles (YPF, Shell, Axion), apps de viaje (Uber, Cabify, Didi) -> "Transporte".
- Apps de delivery (Rappi, PedidosYa, Glovo) -> "Delivery".
- Compras en Mercado Libre / Amazon / AliExpress -> "Compras online".
- Restaurantes, cafeterias, bares, fast food -> "Salidas".
- Suscripciones digitales (Netflix, Spotify, Disney, HBO, YouTube Premium, DLocal) -> "Suscripciones".
- Servicios publicos (luz, gas, agua, telefono, internet) -> "Servicios publicos".
- Rendimientos / inversiones / plazos fijos -> "Inversiones".
- Transferencias enviadas/recibidas P2P -> "Transferencias".
- Cuotas de credito / prestamos -> "Prestamos".

Devolves un JSON array, un item por transaccion, en el mismo orden recibido."""


@dataclass
class GeminiResult:
    classified: int = 0
    unknown: int = 0
    errors: int = 0
    details: list[str] = None

    def __post_init__(self):
        if self.details is None:
            self.details = []

    def summary(self) -> str:
        lines = [
            f"Categorizadas por IA: {self.classified}",
            f"Sin categoria (UNKNOWN): {self.unknown}",
            f"Errores: {self.errors}",
        ]
        if self.details:
            lines.append("")
            lines.append("Detalle:")
            lines.extend(f"- {d}" for d in self.details[:8])
            if len(self.details) > 8:
                lines.append(f"... y {len(self.details) - 8} mas")
        return "\n".join(lines)


def _format_tx_for_prompt(t: dict) -> dict:
    """Extrae los campos relevantes de un grupo de transaccion de Firefly."""
    g = t["attributes"]
    j = g["transactions"][0]  # asumimos 1 split (es lo normal en Firefly)
    return {
        "id": t["id"],
        "external_id": j.get("external_id") or "",
        "date": (j.get("date") or "")[:10],
        "amount": j.get("amount"),
        "type": j.get("type"),
        "description": j.get("description") or "",
    }


def _build_prompt(txs: list[dict], categories: list[str]) -> str:
    return (
        "Categorias disponibles (elegi UNA o 'UNKNOWN'):\n"
        + "\n".join(f"- {c}" for c in categories)
        + "\n\nTransacciones:\n"
        + json.dumps([{"i": t["id"], "desc": t["description"], "amount": t["amount"], "type": t["type"]} for t in txs], ensure_ascii=False)
        + '\n\nResponde un JSON array con objetos {"i": <id>, "category": <nombre o UNKNOWN>}, '
        "manteniendo el orden recibido."
    )


def categorize_pending(
    client: FireflyClient,
    gemini_api_key: str,
    *,
    tag_filter: str = "mercadopago",
    model: str = "gemini-2.0-flash",
    rule_group_id: str | int | None = None,
) -> GeminiResult:
    """Busca transacciones taggeadas con `tag_filter` sin categoria y las clasifica con Gemini."""
    res = GeminiResult()

    # 1. listar candidatos: tag mercadopago + sin categoria + sin tag de IA-miss
    query = f'tag_is:"{tag_filter}" has_no_category:true -tag_is:"ai-miss"'
    txs_raw = client.search_transactions(query, limit=200)
    if not txs_raw:
        log.info("Sin transacciones pendientes con tag=%s", tag_filter)
        return res

    txs = [_format_tx_for_prompt(t) for t in txs_raw]
    log.info("Pendientes a clasificar por IA: %d", len(txs))

    # 2. categorias disponibles
    cats = [c["attributes"]["name"] for c in client.list_categories()]
    if not cats:
        res.errors = len(txs)
        res.details.append("No hay categorias en Firefly. Crear primero.")
        return res

    # 3. llamada a Gemini con structured output (JSON array)
    g = genai.Client(api_key=gemini_api_key)
    prompt = _build_prompt(txs, cats)

    try:
        response = g.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        data = json.loads(response.text)
    except Exception as e:
        log.exception("Gemini fallo")
        res.errors = len(txs)
        res.details.append(f"Gemini error: {e}")
        return res

    # 4. mapear y actualizar Firefly
    by_id = {t["id"]: t for t in txs}
    cats_lower = {c.lower(): c for c in cats}

    for item in data:
        tid = str(item.get("i") or item.get("id") or "")
        cat = (item.get("category") or "").strip()
        if not tid or tid not in by_id:
            continue
        original_desc = by_id[tid]["description"][:50]

        if cat == "UNKNOWN" or not cat:
            try:
                # marcar para no reintentar
                _add_tag(client, tid, "ai-miss")
                res.unknown += 1
                res.details.append(f"UNKNOWN: {original_desc}")
            except Exception as e:
                res.errors += 1
                res.details.append(f"err tag #{tid}: {e}")
            continue

        # validar que la categoria devuelta exista (case-insensitive)
        canonical = cats_lower.get(cat.lower())
        if not canonical:
            log.warning("LLM devolvio categoria desconocida: %r", cat)
            res.unknown += 1
            res.details.append(f"cat invalida '{cat}': {original_desc}")
            continue

        try:
            client.update_transaction_category(tid, canonical)
            _add_tag(client, tid, "ai-classified")
            res.classified += 1
            log.info("  + #%s '%s' -> %s", tid, original_desc, canonical)
        except Exception as e:
            res.errors += 1
            res.details.append(f"err update #{tid}: {e}")

    return res


def _add_tag(client: FireflyClient, group_id: str, tag: str) -> None:
    """Agrega un tag a TODOS los journals del grupo, sin tocar otros campos."""
    data = client._get(f"/api/v1/transactions/{group_id}")
    journals = data["data"]["attributes"]["transactions"]
    new_txs = []
    for j in journals:
        existing = list(j.get("tags") or [])
        if tag in existing:
            continue
        new_txs.append({
            "transaction_journal_id": j["transaction_journal_id"],
            "tags": existing + [tag],
        })
    if not new_txs:
        return
    import requests as _r
    payload = {"apply_rules": False, "fire_webhooks": False, "transactions": new_txs}
    r = _r.put(
        f"{client.base}/api/v1/transactions/{group_id}",
        headers=client._h(content_type=True),
        json=payload,
        timeout=client.timeout,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"PUT tag -> {r.status_code}: {r.text[:200]}")
