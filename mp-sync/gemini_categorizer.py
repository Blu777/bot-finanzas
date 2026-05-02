"""Categorizador de transacciones usando Google Gemini."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime

from google import genai

from firefly_client import FireflyClient
from gemini_config import DEFAULT_GEMINI_MODEL, low_latency_config
from retry_utils import call_with_retries


log = logging.getLogger(__name__)


SYSTEM_PROMPT = (
    "Clasificador de transacciones AR (es). Recibis categorias conocidas y "
    "descripciones (una por linea con indice). Devolves SOLO un JSON array de "
    "strings: el nombre EXACTO de categoria por cada tx en orden. "
    "Usa un nombre conocido o inventa uno nuevo corto si ninguna encaja bien. "
    "Dejar vacio ('') si no hay ni idea."
)


@dataclass
class GeminiResult:
    classified: int = 0
    unknown: int = 0
    errors: int = 0
    details: list[str] = None
    proposed_new: list[str] = None

    def __post_init__(self):
        if self.details is None:
            self.details = []
        if self.proposed_new is None:
            self.proposed_new = []

    def summary(self) -> str:
        lines = [
            f"Categorizadas por IA: {self.classified}",
            f"Sin categoria (UNKNOWN): {self.unknown}",
            f"Errores: {self.errors}",
        ]
        if self.proposed_new:
            lines.append(f"Categorias nuevas propuestas (no creadas): {', '.join(self.proposed_new)}")
        if self.details:
            lines.append("")
            lines.append("Detalle:")
            lines.extend(f"- {d}" for d in self.details[:8])
            if len(self.details) > 8:
                lines.append(f"... y {len(self.details) - 8} mas")
        return "\n".join(lines)


def _format_tx_for_prompt(t: dict) -> dict:
    """Extrae campos utiles para clasificar por patrones (dia/monto/descripcion)."""
    j = t["attributes"]["transactions"][0]
    desc = (j.get("description") or "").strip()
    if len(desc) > 80:
        desc = desc[:80]
    date_raw = (j.get("date") or "")[:10]
    weekday = ""
    if date_raw:
        try:
            # YYYY-MM-DD -> Monday..Sunday
            weekday = datetime.strptime(date_raw, "%Y-%m-%d").strftime("%A")
        except ValueError:
            weekday = ""
    amount_raw = j.get("amount")
    amount_abs = ""
    try:
        amount_abs = f"{abs(float(amount_raw)):.2f}"
    except (TypeError, ValueError):
        amount_abs = ""
    return {
        "id": t["id"],
        "description": desc,
        "date": date_raw,
        "weekday": weekday,
        "amount": amount_abs,
    }


def _build_prompt(txs: list[dict], categories: list[str]) -> str:
    cats_block = "\n".join(f"{i}:{c}" for i, c in enumerate(categories))
    txs_block = "\n".join(
        f"{i}: desc={t['description']} | date={t.get('date','')} | day={t.get('weekday','')} | amount={t.get('amount','')}"
        for i, t in enumerate(txs)
    )
    return f"Categorias:\n{cats_block}\n\nTxs:\n{txs_block}"


def categorize_pending(
    client: FireflyClient,
    gemini_api_key: str,
    *,
    tag_filter: str = "mercadopago",
    model: str = DEFAULT_GEMINI_MODEL,
    rule_group_id: str | int | None = None,
) -> GeminiResult:
    """Busca transacciones taggeadas con `tag_filter` sin categoria y las clasifica con Gemini."""
    model = DEFAULT_GEMINI_MODEL
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

    # 3. llamada a Gemini: output = array de enteros (indice de categoria, -1 = UNKNOWN)
    g = genai.Client(api_key=gemini_api_key)
    prompt = _build_prompt(txs, cats)

    try:
        response = call_with_retries(
            lambda: g.models.generate_content(
                model=model,
                contents=prompt,
                config=low_latency_config(
                    model=model,
                    system_instruction=SYSTEM_PROMPT,
                    response_schema={"type": "ARRAY", "items": {"type": "STRING"}},
                ),
            ),
            attempts=3,
            base_delay=1.0,
            log=log,
            label="Gemini categorize",
        )
        data = json.loads(response.text)
    except Exception as e:
        log.exception("Gemini fallo")
        res.errors = len(txs)
        res.details.append(f"Gemini error: {e}")
        return res

    if not isinstance(data, list) or len(data) != len(txs):
        log.warning("Respuesta de Gemini con largo inesperado: got=%s expected=%d", data, len(txs))
        res.errors = len(txs)
        res.details.append(f"respuesta IA invalida (len={len(data) if isinstance(data, list) else '?'})")
        return res

    # 4. mapear nombre -> categoria y actualizar Firefly
    cats_map = {c.lower(): c for c in cats}
    for tx, cat_name in zip(txs, data):
        tid = tx["id"]
        original_desc = tx["description"][:50]
        canonical = (cat_name or "").strip()

        if not canonical:
            try:
                _add_tag(client, tid, "ai-miss")
                res.unknown += 1
                res.details.append(f"UNKNOWN: {original_desc}")
            except Exception as e:
                res.errors += 1
                res.details.append(f"err tag #{tid}: {e}")
            continue

        if canonical.lower() not in cats_map:
            try:
                _add_tag(client, tid, "ai-miss")
                res.unknown += 1
                res.details.append(f"NUEVA (sin confirmar): {original_desc} -> {canonical}")
                if canonical not in res.proposed_new:
                    res.proposed_new.append(canonical)
            except Exception as e:
                res.errors += 1
                res.details.append(f"err tag #{tid}: {e}")
            continue

        try:
            firefly_cat = cats_map[canonical.lower()]
            client.update_transaction_category(tid, firefly_cat)
            _add_tag(client, tid, "ai-classified")
            res.classified += 1
            log.info("  + #%s '%s' -> %s", tid, original_desc, firefly_cat)
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
    payload = {"apply_rules": False, "fire_webhooks": False, "transactions": new_txs}
    r = client._request(
        "PUT",
        f"/api/v1/transactions/{group_id}",
        headers=client._h(content_type=True),
        json=payload,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"PUT tag -> {r.status_code}: {r.text[:200]}")
