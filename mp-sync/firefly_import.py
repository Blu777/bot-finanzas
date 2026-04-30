"""Importa transacciones de un CSV de Mercado Pago a Firefly III."""
from __future__ import annotations

import csv
import logging
import time
from dataclasses import dataclass, field

from firefly_client import FireflyClient


log = logging.getLogger(__name__)

REQUIRED_COLS = {"Date", "Description", "Amount", "External_ID"}


@dataclass
class ImportResult:
    total: int = 0
    created: int = 0
    skipped: int = 0
    errors: int = 0
    error_details: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"Total filas: {self.total}",
            f"Creadas:     {self.created}",
            f"Ya existian: {self.skipped}",
            f"Errores:     {self.errors}",
        ]
        if self.error_details:
            lines.append("")
            lines.append("Errores:")
            lines.extend(f"- {e}" for e in self.error_details[:5])
            if len(self.error_details) > 5:
                lines.append(f"... y {len(self.error_details) - 5} mas")
        return "\n".join(lines)


def _post_tx(client: FireflyClient, asset_id: int, currency: str, row: dict) -> str:
    eid = f"mp-{row['External_ID'].strip()}"
    if client.transaction_exists(eid):
        return "skip"

    amount_raw = float(row["Amount"])
    is_withdrawal = amount_raw < 0
    amount = f"{abs(amount_raw):.2f}"
    desc = (row["Description"] or "").strip() or "Mercado Pago"
    date = row["Date"].strip()

    tx: dict = {
        "type": "withdrawal" if is_withdrawal else "deposit",
        "date": date,
        "amount": amount,
        "currency_code": currency,
        "description": desc,
        "external_id": eid,
        "tags": ["mercadopago", "import-csv"],
        "notes": f"Importado desde CSV. External_ID original: {row['External_ID'].strip()}",
    }
    if is_withdrawal:
        tx["source_id"] = asset_id
        tx["destination_name"] = desc
    else:
        tx["source_name"] = desc
        tx["destination_id"] = asset_id

    payload = {
        "error_if_duplicate_hash": False,
        "apply_rules": True,
        "fire_webhooks": False,
        "transactions": [tx],
    }
    client.create_transaction(payload)
    return "ok"


def import_csv_file(
    csv_path: str,
    *,
    client: FireflyClient,
    asset_id: int,
    currency: str = "ARS",
) -> ImportResult:
    res = ImportResult()

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or not REQUIRED_COLS.issubset(set(reader.fieldnames)):
            raise ValueError(
                f"CSV invalido. Faltan columnas. Esperadas: {sorted(REQUIRED_COLS)}. "
                f"Encontradas: {reader.fieldnames}"
            )
        rows = list(reader)

    res.total = len(rows)
    log.info("Procesando %d filas (asset_id=%d)", res.total, asset_id)

    for i, row in enumerate(rows, 1):
        eid = row.get("External_ID", "").strip()
        try:
            r = _post_tx(client, asset_id, currency, row)
        except Exception as e:
            res.errors += 1
            res.error_details.append(f"fila {i} (eid={eid}): {e}")
            log.warning("Error fila %d eid=%s: %s", i, eid, e)
            continue
        if r == "ok":
            res.created += 1
        elif r == "skip":
            res.skipped += 1
        time.sleep(0.05)

    return res
