"""Importa transacciones de un CSV de Mercado Pago a Firefly III."""
from __future__ import annotations

import csv
import io
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from firefly_client import FireflyClient


log = logging.getLogger(__name__)

REQUIRED_COLS = {"Date", "Description", "Amount", "External_ID"}
MP_STATEMENT_COLS = {
    "RELEASE_DATE",
    "TRANSACTION_TYPE",
    "REFERENCE_ID",
    "TRANSACTION_NET_AMOUNT",
}


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


def _parse_amount(value: str) -> float:
    raw = (value or "").strip()
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    return float(raw)


def _normalize_date(value: str) -> str:
    raw = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return raw


def _detect_dialect(text: str) -> csv.Dialect:
    sample = text[:4096]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        return csv.excel


def _load_rows(csv_path: str) -> tuple[list[dict], str]:
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        text = f.read()

    dialect = _detect_dialect(text)
    reader = csv.reader(io.StringIO(text), dialect=dialect)
    raw_rows = [[cell.strip() for cell in row] for row in reader if any(cell.strip() for cell in row)]
    if not raw_rows:
        return [], "empty"

    header_idx = None
    for i, row in enumerate(raw_rows):
        cols = set(row)
        if REQUIRED_COLS.issubset(cols) or MP_STATEMENT_COLS.issubset(cols):
            header_idx = i
            break

    if header_idx is None:
        first = raw_rows[0] if raw_rows else []
        raise ValueError(
            "CSV invalido. No encontre un encabezado compatible. "
            f"Esperaba {sorted(REQUIRED_COLS)} o statement MP con {sorted(MP_STATEMENT_COLS)}. "
            f"Primera fila: {first}"
        )

    headers = raw_rows[header_idx]
    data_rows = raw_rows[header_idx + 1 :]

    if REQUIRED_COLS.issubset(set(headers)):
        return [dict(zip(headers, row)) for row in data_rows if len(row) >= len(headers)], "canonical"

    if MP_STATEMENT_COLS.issubset(set(headers)):
        out: list[dict] = []
        for row in data_rows:
            if len(row) < len(headers):
                continue
            item = dict(zip(headers, row))
            ref = (item.get("REFERENCE_ID") or "").strip()
            if not ref:
                continue
            out.append(
                {
                    "Date": _normalize_date(item.get("RELEASE_DATE", "")),
                    "Description": (item.get("TRANSACTION_TYPE") or "").strip() or "Mercado Pago",
                    "Amount": f"{_parse_amount(item.get('TRANSACTION_NET_AMOUNT', '0')):.2f}",
                    "External_ID": ref,
                }
            )
        return out, "mercadopago_statement"

    raise ValueError(f"CSV invalido. Encabezado no compatible: {headers}")


def import_csv_file(
    csv_path: str,
    *,
    client: FireflyClient,
    asset_id: int,
    currency: str = "ARS",
    ledger=None,
) -> ImportResult:
    res = ImportResult()

    rows, source_format = _load_rows(csv_path)
    batch = ledger.create_import(csv_path, source_format) if ledger else None

    res.total = len(rows)
    log.info("Procesando %d filas (asset_id=%d)", res.total, asset_id)

    for i, row in enumerate(rows, 1):
        eid = row.get("External_ID", "").strip()
        try:
            r = _post_tx(client, asset_id, currency, row)
        except Exception as e:
            res.errors += 1
            res.error_details.append(f"fila {i} (eid={eid}): {e}")
            if ledger and batch:
                ledger.record_import_row(batch.id, i, row, status="error", error=str(e))
            log.warning("Error fila %d eid=%s: %s", i, eid, e)
            continue
        if r == "ok":
            res.created += 1
            if ledger and batch:
                ledger.record_import_row(batch.id, i, row, status="created")
        elif r == "skip":
            res.skipped += 1
            if ledger and batch:
                ledger.record_import_row(batch.id, i, row, status="skipped")
        time.sleep(0.05)

    if ledger and batch:
        ledger.finish_import(
            batch.id,
            total=res.total,
            created=res.created,
            skipped=res.skipped,
            errors=res.errors,
        )
    return res
