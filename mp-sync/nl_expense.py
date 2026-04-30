"""Registro de gastos via lenguaje natural.

Flujo:
  texto libre -> LLM extrae {amount,desc,category,date}
              -> buscar match en ledger SQLite (monto + fecha +-1 dia)
              -> si existe y tiene firefly_id: no hacer nada
              -> si existe y NO tiene firefly_id: pushear a Firefly usando
                 la descripcion/categoria del ledger (verdad manual)
              -> si no existe: agregar al ledger y a Firefly
"""
from __future__ import annotations

import csv
import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from google import genai
from google.genai import types

from firefly_client import FireflyClient
from retry_utils import call_with_retries


log = logging.getLogger(__name__)


LEDGER_HEADERS = ["date", "description", "amount", "category", "source", "firefly_id"]


SYSTEM_PROMPT = (
    "Extractor de gastos/ingresos AR (es). Recibis texto informal y categorias "
    "numeradas. Devolves JSON estricto con:\n"
    "- amount: numero SIGNADO. Gasto=NEGATIVO, ingreso=POSITIVO. Si no aclara, "
    "asumi gasto.\n"
    "- description: texto limpio del comercio/concepto (ej: 'Supermercado chino').\n"
    "- category_index: indice 0-based de la categoria que mejor encaje, -1 si ninguna.\n"
    "- date: YYYY-MM-DD. Si no se menciona, usa la fecha de hoy indicada abajo.\n"
    "\n"
    "Conversiones:\n"
    "- 'lucas' o 'k' = miles (15 lucas = 15000, 3k = 3000).\n"
    "- 'palo' = millon.\n"
    "- 'ayer' = dia anterior a hoy, 'anteayer' = 2 dias antes.\n"
    "\n"
    "Si no hay monto claro devolve amount=0."
)


RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "amount": {"type": "NUMBER"},
        "description": {"type": "STRING"},
        "category_index": {"type": "INTEGER"},
        "date": {"type": "STRING"},
    },
    "required": ["amount", "description", "category_index", "date"],
}


@dataclass
class ParsedExpense:
    amount: float   # signed: negativo = gasto
    description: str
    category: str   # "" si UNKNOWN
    date: str       # YYYY-MM-DD


def parse_expense(
    text: str,
    *,
    gemini_api_key: str,
    model: str,
    categories: list[str],
    today: date | None = None,
) -> ParsedExpense:
    """Extrae una transaccion desde texto libre. ~300 tokens input, ~30 output."""
    today = today or date.today()
    prompt = (
        f"Hoy: {today.isoformat()}\n"
        "Categorias:\n"
        + "\n".join(f"{i}:{c}" for i, c in enumerate(categories))
        + f"\n\nTexto: {text.strip()}"
    )

    client = genai.Client(api_key=gemini_api_key)
    resp = call_with_retries(
        lambda: client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
                temperature=0.0,
            ),
        ),
        attempts=3,
        base_delay=1.0,
        log=log,
        label="Gemini expense parser",
    )
    data = json.loads(resp.text)

    amount = float(data.get("amount") or 0)
    description = (data.get("description") or "").strip() or "Gasto"
    idx = int(data.get("category_index", -1))
    category = categories[idx] if 0 <= idx < len(categories) else ""

    dstr = (data.get("date") or today.isoformat()).strip()[:10]
    try:
        datetime.strptime(dstr, "%Y-%m-%d")
    except ValueError:
        dstr = today.isoformat()

    return ParsedExpense(amount=amount, description=description, category=category, date=dstr)


@dataclass
class LedgerRow:
    date: str
    description: str
    amount: float
    category: str = ""
    source: str = "bot"          # "manual" | "bot"
    firefly_id: str = ""
    _row_index: int = -1         # id interno en SQLite


class Ledger:
    """SQLite local como fuente de verdad manual.

    Mantiene compatibilidad con LOCAL_LEDGER_CSV: si llega /data/ledger.csv,
    usa /data/ledger.sqlite y migra las filas del CSV legacy si existen.
    """

    def __init__(self, path: str | Path):
        requested = Path(path)
        self.legacy_csv_path = requested if requested.suffix.lower() == ".csv" else None
        self.path = requested.with_suffix(".sqlite") if requested.suffix.lower() == ".csv" else requested
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._migrate_legacy_csv()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _db(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    description TEXT NOT NULL,
                    amount REAL NOT NULL,
                    category TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'bot',
                    firefly_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_ledger_amount_date
                ON ledger_entries(amount, date)
                """
            )

    def _migrate_legacy_csv(self) -> None:
        if self.legacy_csv_path is None or not self.legacy_csv_path.exists():
            return
        with self._db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0]
            if count:
                return
            rows = []
            with self.legacy_csv_path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        amt = float((row.get("amount") or "0").replace(",", "."))
                    except ValueError:
                        amt = 0.0
                    rows.append(
                        (
                            (row.get("date") or "").strip(),
                            (row.get("description") or "").strip(),
                            amt,
                            (row.get("category") or "").strip(),
                            (row.get("source") or "manual").strip(),
                            (row.get("firefly_id") or "").strip(),
                        )
                    )
            if rows:
                conn.executemany(
                    """
                    INSERT INTO ledger_entries
                    (date, description, amount, category, source, firefly_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                log.info("Migradas %d filas de %s a %s", len(rows), self.legacy_csv_path, self.path)

    def _read_all(self) -> list[LedgerRow]:
        out: list[LedgerRow] = []
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT id, date, description, amount, category, source, firefly_id
                FROM ledger_entries
                ORDER BY id
                """
            ).fetchall()
            for row in rows:
                out.append(
                    LedgerRow(
                        date=(row["date"] or "").strip(),
                        description=(row["description"] or "").strip(),
                        amount=float(row["amount"] or 0),
                        category=(row["category"] or "").strip(),
                        source=(row["source"] or "manual").strip(),
                        firefly_id=(row["firefly_id"] or "").strip(),
                        _row_index=int(row["id"]),
                    )
                )
        return out

    def find_match(
        self, amount: float, date_str: str, *, tolerance_days: int = 1
    ) -> LedgerRow | None:
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return None
        amt_key = round(amount, 2)
        best: LedgerRow | None = None
        best_delta = tolerance_days + 1
        for r in self._read_all():
            if round(r.amount, 2) != amt_key:
                continue
            try:
                rd = datetime.strptime(r.date, "%Y-%m-%d").date()
            except ValueError:
                continue
            delta = abs((rd - target).days)
            if delta <= tolerance_days and delta < best_delta:
                best = r
                best_delta = delta
        return best

    def append(self, row: LedgerRow) -> int:
        with self._db() as conn:
            cur = conn.execute(
                """
                INSERT INTO ledger_entries
                (date, description, amount, category, source, firefly_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row.date,
                    row.description,
                    float(row.amount),
                    row.category,
                    row.source,
                    row.firefly_id,
                ),
            )
            return int(cur.lastrowid)

    def update_row(self, row_index: int, **fields) -> None:
        allowed = {"date", "description", "amount", "category", "source", "firefly_id"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [row_index]
        with self._db() as conn:
            cur = conn.execute(
                f"UPDATE ledger_entries SET {assignments} WHERE id = ?",
                values,
            )
        if cur.rowcount == 0:
            log.warning("update_row: id inexistente %s", row_index)
            return

    def delete_last(self) -> LedgerRow | None:
        """Borra la ultima fila de datos y la devuelve. None si no hay datos."""
        with self._db() as conn:
            row = conn.execute(
                """
                SELECT id, date, description, amount, category, source, firefly_id
                FROM ledger_entries
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM ledger_entries WHERE id = ?", (row["id"],))
            return LedgerRow(
                date=(row["date"] or "").strip(),
                description=(row["description"] or "").strip(),
                amount=float(row["amount"] or 0),
                category=(row["category"] or "").strip(),
                source=(row["source"] or "manual").strip(),
                firefly_id=(row["firefly_id"] or "").strip(),
                _row_index=int(row["id"]),
            )


@dataclass
class RecordResult:
    action: str           # "created" | "synced_from_csv" | "already_synced" | "noop"
    row: LedgerRow
    message: str = ""

    def summary(self) -> str:
        r = self.row
        sign = "-" if r.amount < 0 else "+"
        cat = r.category or "(sin categoria)"
        return (
            f"[{self.action}] {r.date}  {sign}${abs(r.amount):,.2f}  "
            f"{r.description}  [{cat}]"
            + (f"  firefly#{r.firefly_id}" if r.firefly_id else "")
            + (f"\n{self.message}" if self.message else "")
        )


def record_expense(
    parsed: ParsedExpense,
    *,
    ledger: Ledger,
    firefly: FireflyClient,
    asset_id: int,
    currency: str = "ARS",
) -> RecordResult:
    if parsed.amount == 0:
        return RecordResult(
            action="noop",
            row=LedgerRow(
                date=parsed.date,
                description=parsed.description,
                amount=0,
                category=parsed.category,
            ),
            message="No se reconocio un monto.",
        )

    match = ledger.find_match(parsed.amount, parsed.date, tolerance_days=1)

    if match is not None:
        # Fecha: corregir si difiere (metadato tecnico), contenido intacto.
        if match.date != parsed.date:
            ledger.update_row(match._row_index, date=parsed.date)
            match.date = parsed.date

        if match.firefly_id:
            return RecordResult(
                action="already_synced",
                row=match,
                message="Ya existia en CSV y en Firefly.",
            )

        # Existe en CSV pero no en Firefly. La descripcion/categoria del CSV ganan.
        fid = _push_firefly(match, firefly=firefly, asset_id=asset_id, currency=currency)
        ledger.update_row(match._row_index, firefly_id=fid)
        match.firefly_id = fid
        return RecordResult(
            action="synced_from_csv",
            row=match,
            message="Uso la version manual del CSV (no sobreescribo).",
        )

    # Nueva entrada
    new_row = LedgerRow(
        date=parsed.date,
        description=parsed.description,
        amount=parsed.amount,
        category=parsed.category,
        source="bot",
    )
    fid = _push_firefly(new_row, firefly=firefly, asset_id=asset_id, currency=currency)
    new_row.firefly_id = fid
    idx = ledger.append(new_row)
    new_row._row_index = idx
    return RecordResult(
        action="created",
        row=new_row,
        message="Agregado al CSV y a Firefly.",
    )


def _push_firefly(
    row: LedgerRow,
    *,
    firefly: FireflyClient,
    asset_id: int,
    currency: str,
) -> str:
    is_withdrawal = row.amount < 0
    amount_abs = f"{abs(row.amount):.2f}"
    desc = row.description or ("Gasto" if is_withdrawal else "Ingreso")

    tx: dict = {
        "type": "withdrawal" if is_withdrawal else "deposit",
        "date": row.date,
        "amount": amount_abs,
        "currency_code": currency,
        "description": desc,
        "tags": ["telegram-bot", "nl"],
        "notes": "Registrado via Telegram bot (lenguaje natural).",
    }
    if row.category:
        tx["category_name"] = row.category
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
    resp = firefly.create_transaction(payload)
    data = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(data, dict):
        return str(data.get("id") or "")
    return ""
