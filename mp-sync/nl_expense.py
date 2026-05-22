"""Registro de gastos via lenguaje natural.

Flujo:
  texto libre -> LLM extrae {monto,descripcion,categoria,cuenta,tipo,fecha}
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
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from google import genai

from firefly_client import FireflyClient, FireflyError
from gemini_config import DEFAULT_GEMINI_MODEL, low_latency_config
from retry_utils import call_with_retries


log = logging.getLogger(__name__)


LEDGER_HEADERS = [
    "date", "description", "amount", "category", "account", "tx_type", "source", "firefly_id",
]


SYSTEM_PROMPT = (
    "Extractor de movimientos financieros AR (es). Recibis texto informal, "
    "categorias conocidas y cuentas asset conocidas. Devolves JSON estricto con "
    "esta estructura exacta:\n"
    '{"monto": float, "descripcion": str, "categoria": str, "cuenta": str, '
    '"tipo": "ingreso"|"gasto"|"transferencia", "cuenta_destino": str, "fecha": "ISO-8601"}\n'
    "- monto: numero positivo sin signo. El sentido del dinero va en tipo.\n"
    "- tipo: 'gasto' si sale dinero de la cuenta asset; 'ingreso' si entra dinero "
    "a la cuenta asset; 'transferencia' si mueve dinero entre cuentas asset. "
    "Si no aclara, asumi gasto.\n"
    "- cuenta: cuenta asset de ORIGEN (de donde sale el dinero). Usar alias corto "
    "de las cuentas conocidas (ej: Efectivo, Banco, MP). Si no se menciona, dejar "
    "vacio para que el bot use la cuenta por defecto.\n"
    "- cuenta_destino: cuenta asset de DESTINO (a donde llega el dinero). Solo "
    "para tipo=transferencia. Si no se menciona, dejar vacio.\n"
    "- IMPORTANTE: si el usuario pone un signo explicito (- o +) antes del monto, "
    "RESPETAR ese signo para tipo siempre (excepto transferencia). Ej: '-4000' = gasto, '+4000' = ingreso.\n"
    "- 'devolucion' / 'devolver' cuando el usuario devuelve dinero = gasto, "
    "no ingreso.\n"
    "- 'retiro' / 'extraccion' de una cuenta = transferencia hacia Efectivo.\n"
    "- 'deposito' en una cuenta = transferencia desde Efectivo (u origen default).\n"
    "- descripcion: descripcion LIMPIA, CAPITALIZADA y bien redactada del "
    "comercio o concepto. Primera letra de cada palabra significativa en "
    "MAYUSCULA (Title Case). Ejemplos:\n"
    "  'comida trabajo tarta de pollo' -> 'Tarta de Pollo (Almuerzo Trabajo)'\n"
    "  'nafta ypf' -> 'Nafta YPF'\n"
    "  'super chino' -> 'Supermercado Chino'\n"
    "  'uber casa trabajo' -> 'Uber Casa-Trabajo'\n"
    "  'regalo cumple juan' -> 'Regalo Cumpleaños Juan'\n"
    "  Abreviaturas comunes se expanden: 'super'->'Supermercado', "
    "'cumple'->'Cumpleaños', 'depto'->'Departamento', 'farma'->'Farmacia'.\n"
    "  Agregar contexto entre parentesis si el usuario lo menciona.\n"
    "- categoria: nombre EXACTO de categoria conocida que mejor encaje. "
    "Si ninguna encaja bien, inventa un nombre nuevo corto y descriptivo "
    "(ej: 'Kiosco', 'Barberia'). Deja vacio si no hay ni idea.\n"
    "- fecha: YYYY-MM-DD. Si no se menciona, usa la fecha de hoy indicada abajo.\n"
    "\n"
    "Conversiones:\n"
    "- 'lucas' o 'k' = miles (15 lucas = 15000, 3k = 3000).\n"
    "- 'palo' = millon.\n"
    "- Frases como 'me entro', 'me entraron', 'cobre', 'recibi', 'deposito en', "
    "'transferencia a mi cuenta' suelen ser ingresos.\n"
    "- Frases como 'pague', 'compre', 'gaste', 'mande', 'transferi a otra persona' "
    "suelen ser gastos.\n"
    "- 'ayer' = dia anterior a hoy, 'anteayer' = 2 dias antes.\n"
    "\n"
    "Si no hay monto claro devolve monto=0."
)


RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "amount": {"type": "NUMBER"},
        "description": {"type": "STRING"},
        "category_name": {"type": "STRING"},
        "date": {"type": "STRING"},
        "monto": {"type": "NUMBER"},
        "descripcion": {"type": "STRING"},
        "categoria": {"type": "STRING"},
        "cuenta": {"type": "STRING"},
        "tipo": {"type": "STRING", "enum": ["ingreso", "gasto", "transferencia"]},
        "cuenta_destino": {"type": "STRING"},
        "fecha": {"type": "STRING"},
    },
    "required": ["monto", "descripcion", "categoria", "cuenta", "tipo", "fecha"],
}


@dataclass
class ParsedExpense:
    amount_cents: int  # Primary: signed integer cents (100 = $1.00). Negative = gasto
    description: str
    category: str   # "" si UNKNOWN
    date: str       # YYYY-MM-DD
    account: str = ""  # alias de cuenta asset origen, ej: Efectivo, Banco, MP
    account_dest: str = ""  # alias cuenta destino (solo transferencia)
    tx_type: str = "gasto"
    currency: str = "ARS"
    confidence: float = 1.0
    needs_confirmation: bool = False
    warnings: list[str] = field(default_factory=list)
    
    @property
    def amount(self) -> float:
        """Legacy accessor - returns float for backward compatibility (display only)."""
        return self.amount_cents / 100.0
    
    @classmethod
    def from_float(cls, amount_float: float, **kwargs):
        """Factory method to create from float (for gradual migration)."""
        return cls(amount_cents=int(round(amount_float * 100)), **kwargs)


@dataclass
class ParseResult:
    transactions: list[ParsedExpense] = field(default_factory=list)
    confidence: float = 0.0
    needs_confirmation: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


_EXPLICIT_SIGN_RE = re.compile(
    r"(?:^|[\s(])"           # start of string or whitespace/paren
    r"([+-])"                # explicit sign
    r"\s*"
    r"(\d[\d.,]*)"           # digits (possibly with . or , separators)
    r"\s*"
    r"(?:k|lucas|palo)?"     # optional multiplier
    r"(?:[\s),;]|$)",        # word boundary
    re.IGNORECASE,
)


def _enforce_explicit_sign(raw_text: str, llm_amount: float) -> float:
    """Override LLM amount sign when the user typed an explicit +/- prefix."""
    if llm_amount == 0:
        return llm_amount
    m = _EXPLICIT_SIGN_RE.search(raw_text)
    if not m:
        return llm_amount
    sign_char = m.group(1)
    if sign_char == "-" and llm_amount > 0:
        return -llm_amount
    if sign_char == "+" and llm_amount < 0:
        return -llm_amount
    return llm_amount


_QP_AMOUNT_RE = re.compile(
    r"(?:^|(?<=\s))"
    r"([+-])?"
    r"(\d{1,10})"
    r"(?!\s*[.,]\d)"
    r"(?:\s*(k|lucas|palo))?"
    r"(?=\s|$)",
    re.IGNORECASE,
)

_QP_DATE_BAIL_RE = re.compile(
    r"\b(?:"
    r"ayer|anteayer|ma[n\u00f1]ana"
    r"|lunes|martes|mi[e\u00e9]rcoles|jueves|viernes|s[a\u00e1]bado|domingo"
    r"|semana|mes"
    r"|\d{4}-\d{2}-\d{2}"
    r")\b",
    re.IGNORECASE,
)

_QP_MULTIPLIERS: dict[str, int] = {"k": 1_000, "lucas": 1_000, "palo": 1_000_000}
_QP_STRIP_RE = re.compile(r"\bhoy\b", re.IGNORECASE)


def _try_quick_parse(text: str, today: date, categories: list[str] | None = None) -> ParsedExpense | None:
    """Parsea expresiones simples (entero + descripcion) sin llamar a Gemini.

    Retorna None si el texto es ambiguo, contiene fechas relativas o decimales.
    Casos soportados:  'cafe 150'  '+500 sueldo'  '30k nafta'  '2 lucas uber'  'hoy cafe 150'
    """
    from money_utils import parse_amount_to_cents
    
    t = _QP_STRIP_RE.sub("", text.strip()).strip()
    if not t:
        return None
    if _QP_DATE_BAIL_RE.search(t):
        return None
    matches = list(_QP_AMOUNT_RE.finditer(t))
    if len(matches) != 1:
        return None
    m = matches[0]
    sign_char = m.group(1)
    raw_num = m.group(2)
    mult_str = (m.group(3) or "").lower()
    mult = _QP_MULTIPLIERS.get(mult_str, 1)
    
    # Parse to cents using safe utility
    try:
        amount_cents = parse_amount_to_cents(f"{raw_num}{mult_str}")
        if amount_cents is None:
            return None
    except Exception:
        # Fallback: try legacy parsing and convert
        try:
            amount_float = float(raw_num) * mult
            amount_cents = int(round(amount_float * 100))
        except ValueError:
            return None
    
    if amount_cents == 0:
        return None
        
    before = t[: m.start()].strip()
    after = t[m.end() :].strip()
    desc_raw = f"{before} {after}".strip()
    if not desc_raw:
        return None
    if any(re.match(r"[\d.,]", tok) for tok in desc_raw.split()):
        return None
    description = desc_raw.title()
    
    if sign_char == "+":
        tx_type = "ingreso"
        signed_cents = amount_cents
    else:
        tx_type = "gasto"
        signed_cents = -amount_cents
        
    category = _canonical_category(description, categories or [])
    return ParsedExpense(
        amount_cents=signed_cents,
        description=description,
        category=category,
        date=today.isoformat(),
        tx_type=tx_type,
    )


_AMOUNT_TOKEN_RE = re.compile(
    r"(?:^|(?<=\s))"
    r"(?P<currency_before>u\$s|us\$|usd|ars|\$)?"
    r"\s*"
    r"(?P<sign>[+-])?"
    r"\s*"
    r"(?P<number>\d+(?:[.,]\d{3})*(?:[.,]\d+)?|\d+)"
    r"\s*"
    r"(?P<multiplier>k|lucas?|mil|palo|palos)?"
    r"\s*"
    r"(?P<currency_after>u\$s|us\$|usd|ars|pesos?|d[oó]lares?)?"
    r"(?=\s|$|[,;.!?])",
    re.IGNORECASE,
)
_DATE_WORDS_RE = re.compile(
    r"\b(hoy|ayer|anteayer|ma[nñ]ana|lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\b",
    re.IGNORECASE,
)
_DATE_SLASH_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b")
_CONNECTOR_RE = re.compile(r"\s*(?:,|;|\s+y\s+|\s+e\s+|\s+adem[aá]s\s+|\s+tambi[eé]n\s+)\s*", re.IGNORECASE)
_TRANSFER_BETWEEN_RE = re.compile(r"\b(?:de|desde)\s+(.+?)\s+(?:a|hacia)\s+(.+?)\b", re.IGNORECASE)
_INCOME_RE = re.compile(r"\b(?:me\s+entr(?:o|ó|aron)|cobr[eé]|recib[ií]|me\s+pagaron|sueldo|honorarios|ingreso)\b", re.IGNORECASE)
_EXPENSE_RE = re.compile(r"\b(?:gast[eé]|pagu[eé]|compr[eé]|mand[eé]|transfer[ií]\s+a|alquiler)\b", re.IGNORECASE)
_TRANSFER_RE = re.compile(r"\b(?:transfer(?:i|í|encia)|pas[eé]|mov[ií]|retir[eé]|saqu[eé]|extracci[oó]n|deposit[eé])\b", re.IGNORECASE)
_INSTALLMENTS_RE = re.compile(r"\b(?:en\s+)?(\d{1,2})\s*(?:cuotas?|x)\b|\bcuota\s+(\d{1,2})\s*/\s*(\d{1,2})\b", re.IGNORECASE)
_CATEGORY_HINTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    # Transporte
    (("uber", "cabify", "taxi", "nafta", "ypf", "shell", "axion", "peaje", "sube", "colectivo", "subte", "estacionamiento"), ("Transporte",)),
    # Delivery
    (("delivery", "pedido", "rappi", "pedidosya", "glovo"), ("Delivery",)),
    # Salidas (restaurantes, bares, comida fuera)
    (("sushi", "restaurant", "restaurante", "pizzeria", "cafe", "bar", "restoran", "bodegon", "cantina", "hamburgueseria"), ("Salidas",)),
    # Supermercado
    (("super", "supermercado", "chino", "verduleria", "carniceria", "almacen", "kiosco", "mayorista", "carrefour", "coto", "disco", "jumbo", "dia", "changomas", "mayonesa", "pan", "leche", "huevos", "queso", "yerba", "azucar", "arroz", "fideos", "galletitas", "milanesa", "milanesas", "pollo", "carne", "verdura", "fruta", "aceite", "manteca"), ("Supermercado",)),
    # Comida Trabajo
    (("hestia", "toldito", "vianda", "almuerzo trabajo", "comida trabajo"), ("Comida Trabajo",)),
    # Personal (peluqueria, cuidado personal)
    (("peluqueria", "peluquero", "barberia", "barbero", "corte de pelo", "estetica", "manicuria", "depilacion", "spa"), ("Personal",)),
    # Farmacia / salud
    (("farmacia", "farma", "medicamento", "remedio", "medico", "doctor", "clinica", "hospital", "laboratorio", "dentista"), ("Farmacia",)),
    # Deportes
    (("gym", "gimnasio", "cancha", "pileta", "natacion", "deporte", "rugby", "tenis", "padel", "running", "fitness"), ("Deportes",)),
    # Futbol
    (("futbol", "pelota", "botines", "cuota futbol", "river", "boca", "racing", "independiente"), ("Futbol",)),
    # Servicios
    (("luz", "edesur", "edenor", "gas", "metrogas", "agua", "aysa", "internet", "fibertel", "telecentro", "personal", "movistar", "claro", "telefono", "celular", "cablevision"), ("Servicios",)),
    # Subscripciones
    (("netflix", "spotify", "disney", "hbo", "amazon prime", "youtube", "apple", "icloud", "openai", "chatgpt"), ("Subscripciones",)),
    # Compras online
    (("mercadolibre", "meli", "amazon", "aliexpress", "tiendamia", "shein"), ("Compras online",)),
    # Alquiler+Expensas
    (("alquiler", "expensas", "consorcio"), ("Alquiler+Expensas",)),
    # Prestamos
    (("prestamo", "cuota prestamo", "devolucion prestamo", "fondo"), ("Prestamos",)),
    # Inversiones
    (("inversion", "plazo fijo", "fci", "dolar", "cripto", "bitcoin", "cedear", "accion", "bono"), ("Inversiones",)),
    # Transferencias
    (("transferencia", "transferi", "mande plata"), ("Transferencias",)),
    # Movimientos Internos
    (("recarga", "extraje", "retiro", "deposito efectivo", "dinero disponible"), ("Movimientos Internos",)),
    # Sueldo / ingresos
    (("sueldo", "honorarios", "cobro", "salario"), ("Sueldo",)),
    # Regalos
    (("regalo", "regalos", "presente", "cumple", "cumpleanos", "navidad", "flores"), ("Regalos",)),
    # Salud (medicos, turnos, analisis - distinto de farmacia/remedios)
    (("medico", "doctor", "turno medico", "clinica", "hospital", "laboratorio", "dentista", "odontologo", "kinesiologo", "psicologo", "terapeuta", "analisis", "radiografia", "prepaga", "obra social"), ("Salud",)),
    # Educacion
    (("curso", "capacitacion", "libro", "libros", "universidad", "colegio", "instituto", "clases", "udemy", "coursera", "educacion", "material escolar"), ("Educación",)),
    # Ropa
    (("ropa", "remera", "pantalon", "zapatillas", "calzado", "camisa", "vestido", "abrigo", "campera", "buzo", "remerita", "indumentaria", "zara", "h&m", "adidas", "nike"), ("Ropa",)),
)
_CURRENCY_ALIASES = {
    "$": "ARS",
    "ars": "ARS",
    "peso": "ARS",
    "pesos": "ARS",
    "usd": "USD",
    "us$": "USD",
    "u$s": "USD",
    "dolar": "USD",
    "dólar": "USD",
    "dolares": "USD",
    "dólares": "USD",
}
_MULTIPLIERS = {"k": Decimal("1000"), "luca": Decimal("1000"), "lucas": Decimal("1000"), "mil": Decimal("1000"), "palo": Decimal("1000000"), "palos": Decimal("1000000")}
_WEEKDAYS = {
    "lunes": 0,
    "martes": 1,
    "miercoles": 2,
    "miércoles": 2,
    "jueves": 3,
    "viernes": 4,
    "sabado": 5,
    "sábado": 5,
    "domingo": 6,
}


def _decimal_from_locale(raw: str) -> Decimal | None:
    s = raw.strip()
    if not s:
        return None
    if "." in s and "," in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "." in s:
        parts = s.split(".")
        if len(parts[-1]) == 3 and all(p.isdigit() for p in parts):
            s = "".join(parts)
    elif "," in s:
        parts = s.split(",")
        if len(parts[-1]) == 3 and all(p.isdigit() for p in parts):
            s = "".join(parts)
        else:
            s = s.replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _canonical_currency(raw: str, default_currency: str) -> str:
    return _CURRENCY_ALIASES.get(_strip_accents(raw.strip().lower()), default_currency.upper())


def _title_description(value: str) -> str:
    value = re.sub(r"\b(?:gaste|gasté|pague|pagué|compre|compré|en|con|por|hoy|ayer|anteayer)\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip(" -:,.")
    return value.title() if value else "Movimiento"


def _resolve_relative_date(text: str, today: date) -> tuple[str, list[str], bool]:
    warnings: list[str] = []
    lower = text.lower()
    if "anteayer" in lower:
        return (today - timedelta(days=2)).isoformat(), warnings, False
    if "ayer" in lower:
        return (today - timedelta(days=1)).isoformat(), warnings, False
    if re.search(r"\bma[nñ]ana\b", lower):
        warnings.append("Fecha futura detectada.")
        return (today + timedelta(days=1)).isoformat(), warnings, True
    m = _DATE_SLASH_RE.search(text)
    if m:
        day = int(m.group(1))
        month = int(m.group(2))
        year_raw = m.group(3)
        year = today.year if not year_raw else int(year_raw)
        if year < 100:
            year += 2000
        try:
            parsed = date(year, month, day)
            return parsed.isoformat(), warnings, parsed > today
        except ValueError:
            warnings.append("Fecha invalida; use hoy.")
            return today.isoformat(), warnings, True
    norm = _strip_accents(lower)
    for name, weekday in _WEEKDAYS.items():
        if re.search(rf"\b{re.escape(_strip_accents(name))}\b", norm):
            delta = (today.weekday() - weekday) % 7
            if delta == 0:
                warnings.append("Dia de semana ambiguo; use hoy.")
            return (today - timedelta(days=delta)).isoformat(), warnings, delta == 0
    return today.isoformat(), warnings, False


def _find_account_alias(text: str, account_aliases: list[str]) -> str:
    norm = _strip_accents(text.lower())
    for alias in sorted(account_aliases, key=len, reverse=True):
        if alias == "default":
            continue
        needle = _strip_accents(alias.lower())
        if needle and re.search(rf"\b{re.escape(needle)}\b", norm):
            return alias
    return ""


def _canonical_category(description: str, categories: list[str]) -> str:
    if not categories:
        return ""
    by_lower = {c.lower(): c for c in categories}
    desc_norm = _strip_accents(description.lower())
    for cat in categories:
        cat_norm = _strip_accents(cat.lower())
        if cat_norm and re.search(rf"\b{re.escape(cat_norm)}\b", desc_norm):
            return cat
    for words, candidates in _CATEGORY_HINTS:
        if any(re.search(rf"\b{re.escape(word)}\b", desc_norm) for word in words):
            for candidate in candidates:
                if candidate.lower() in by_lower:
                    return by_lower[candidate.lower()]
    return ""


def _strip_account_alias(desc: str, alias: str) -> str:
    """Elimina el alias de cuenta de la descripcion (case/accent-insensitive, palabra completa)."""
    if not alias:
        return desc
    pattern = rf"\b{re.escape(_strip_accents(alias))}\b"
    cleaned = re.sub(pattern, " ", _strip_accents(desc), flags=re.IGNORECASE)
    # reconstruir con casing original usando posiciones
    # mas simple: aplicar el patron directamente sobre desc con acento-strip solo en el patron
    cleaned = re.sub(
        rf"(?i)\b{re.escape(alias)}\b", " ", desc
    )
    # fallback sin acentos
    if alias.lower() in desc.lower():
        cleaned = re.sub(rf"(?i)\b{re.escape(alias)}\b", " ", desc)
    return re.sub(r"\s+", " ", cleaned).strip()


def _strip_parser_noise(segment: str, amount_match: re.Match) -> str:
    before = segment[: amount_match.start()].strip()
    after = segment[amount_match.end() :].strip()
    desc = f"{before} {after}".strip()
    desc = _DATE_WORDS_RE.sub(" ", desc)
    desc = _DATE_SLASH_RE.sub(" ", desc)
    desc = _INSTALLMENTS_RE.sub(" ", desc)
    return re.sub(r"\s+", " ", desc).strip()


def _split_candidate_segments(text: str) -> list[str]:
    parts = [p.strip() for p in _CONNECTOR_RE.split(text) if p.strip()]
    if len(parts) <= 1:
        return [text.strip()]
    if all(_AMOUNT_TOKEN_RE.search(part) for part in parts):
        return parts
    return [text.strip()]


def _amount_matches(segment: str) -> list[re.Match]:
    installment_spans = [m.span() for m in _INSTALLMENTS_RE.finditer(segment)]
    matches = []
    for match in _AMOUNT_TOKEN_RE.finditer(segment):
        start, end = match.span()
        if any(start >= span_start and end <= span_end for span_start, span_end in installment_spans):
            continue
        matches.append(match)
    return matches


def _try_rule_parse_one(
    segment: str,
    *,
    today: date,
    categories: list[str],
    account_aliases: list[str],
    default_currency: str,
) -> ParsedExpense | None:
    matches = _amount_matches(segment)
    if len(matches) != 1:
        return None
    m = matches[0]
    raw_number = m.group("number")
    value = _decimal_from_locale(raw_number)
    if value is None:
        return None
    multiplier = (m.group("multiplier") or "").lower()
    value *= _MULTIPLIERS.get(multiplier, Decimal("1"))
    if value <= 0:
        return None
    currency_raw = m.group("currency_before") or m.group("currency_after") or ""
    currency = _canonical_currency(currency_raw, default_currency)
    desc_raw = _strip_parser_noise(segment, m)
    if not desc_raw:
        return None
    date_str, warnings, date_needs_confirmation = _resolve_relative_date(segment, today)
    sign = m.group("sign") or ""
    desc_norm = _strip_accents(desc_raw.lower())
    segment_norm = _strip_accents(segment.lower())
    account = _find_account_alias(segment, account_aliases)
    account_dest = ""
    tx_type = "gasto"
    confidence = 0.92
    needs_confirmation = date_needs_confirmation
    if sign == "+" or _INCOME_RE.search(segment_norm):
        tx_type = "ingreso"
    if sign == "-" or _EXPENSE_RE.search(segment_norm):
        tx_type = "gasto"
    if _TRANSFER_RE.search(segment_norm):
        transfer_match = _TRANSFER_BETWEEN_RE.search(segment)
        if transfer_match:
            src = _find_account_alias(transfer_match.group(1), account_aliases)
            dst = _find_account_alias(transfer_match.group(2), account_aliases)
            if src and dst and src != dst:
                account = src
                account_dest = dst
                tx_type = "transferencia"
                confidence = 0.95
            else:
                warnings.append("Transferencia ambigua; se registra como gasto/ingreso.")
                needs_confirmation = True
                confidence = 0.74
        elif "retiro" in segment_norm or "extraccion" in segment_norm or "saque" in segment_norm:
            cash = _find_account_alias("Efectivo", account_aliases)
            if account and cash and account != cash:
                account_dest = cash
                tx_type = "transferencia"
            else:
                needs_confirmation = True
        elif "deposit" in segment_norm:
            cash = _find_account_alias("Efectivo", account_aliases)
            if account and cash and account != cash:
                account_dest = account
                account = cash
                tx_type = "transferencia"
            else:
                needs_confirmation = True
        else:
            warnings.append("Transferencia a persona externa o incompleta.")
            needs_confirmation = True
            confidence = min(confidence, 0.78)
    if _INSTALLMENTS_RE.search(segment):
        warnings.append("Cuotas detectadas; revisar antes de guardar.")
        needs_confirmation = True
        confidence = min(confidence, 0.82)
    if account:
        desc_raw = _strip_account_alias(desc_raw, account)
    description = _title_description(desc_raw)
    category = _canonical_category(f"{description} {desc_norm}", categories)
    
    # Convert Decimal to integer cents
    amount_cents = int((value * 100).to_integral_value())
    if tx_type == "transferencia":
        signed_cents = amount_cents
    else:
        signed_cents = amount_cents if tx_type == "ingreso" else -amount_cents
        
    return ParsedExpense(
        amount_cents=signed_cents,
        description=description,
        category=category,
        date=date_str,
        account=account,
        account_dest=account_dest,
        tx_type=tx_type,
        currency=currency,
        confidence=confidence,
        needs_confirmation=needs_confirmation,
        warnings=warnings,
    )


def parse_expenses(
    text: str,
    *,
    gemini_api_key: str,
    model: str,
    categories: list[str],
    account_aliases: list[str] | None = None,
    today: date | None = None,
    default_currency: str = "ARS",
) -> ParseResult:
    today = today or date.today()
    account_aliases = account_aliases or []
    segments = _split_candidate_segments(text)
    parsed: list[ParsedExpense] = []
    if segments:
        for segment in segments:
            item = _try_rule_parse_one(
                segment,
                today=today,
                categories=categories,
                account_aliases=account_aliases,
                default_currency=default_currency,
            )
            if item is None:
                parsed = []
                break
            parsed.append(item)
    if parsed:
        if (
            len(parsed) == 1
            and parsed[0].tx_type == "gasto"
            and not parsed[0].needs_confirmation
            and categories
            and not parsed[0].category
        ):
            log.debug("rule-parse sin categoria; fallback a Gemini: %r", text)
        else:
            needs_confirmation = len(parsed) > 1 or any(p.needs_confirmation for p in parsed)
            warnings = [w for p in parsed for w in p.warnings]
            confidence = min(p.confidence for p in parsed)
            return ParseResult(parsed, confidence, needs_confirmation, warnings)
    one = parse_expense(
        text,
        gemini_api_key=gemini_api_key,
        model=model,
        categories=categories,
        account_aliases=account_aliases,
        today=today,
        default_currency=default_currency,
        _skip_rule_parse=True,
    )
    return ParseResult([one], one.confidence, one.needs_confirmation, list(one.warnings))


def parse_expense(
    text: str,
    *,
    gemini_api_key: str,
    model: str,
    categories: list[str],
    account_aliases: list[str] | None = None,
    today: date | None = None,
    default_currency: str = "ARS",
    _skip_rule_parse: bool = False,
) -> ParsedExpense:
    """Extrae una transaccion desde texto libre. ~300 tokens input, ~30 output."""
    today = today or date.today()
    if not _skip_rule_parse:
        rule_result = parse_expenses(
            text,
            gemini_api_key=gemini_api_key,
            model=model,
            categories=categories,
            account_aliases=account_aliases,
            today=today,
            default_currency=default_currency,
        )
        if rule_result.transactions:
            return rule_result.transactions[0]
    quick = _try_quick_parse(text, today, categories)
    if quick is not None:
        if quick.category or quick.tx_type != "gasto" or not categories:
            log.debug("quick-parse ok (LLM skipped): %r -> %.2f desc=%r", text, quick.amount, quick.description)
            quick.currency = default_currency
            return quick
        log.debug("quick-parse sin categoria; fallback a Gemini: %r", text)
    account_aliases = account_aliases or []
    prompt = (
        f"Hoy: {today.isoformat()}\n"
        "Categorias:\n"
        + "\n".join(f"{i}:{c}" for i, c in enumerate(categories))
        + "\n\nCuentas asset conocidas:\n"
        + "\n".join(f"- {a}" for a in account_aliases)
        + f"\n\nTexto: {text.strip()}"
    )

    client = genai.Client(api_key=gemini_api_key)
    resp = call_with_retries(
        lambda: client.models.generate_content(
            model=model,
            contents=prompt,
            config=low_latency_config(
                model=model,
                system_instruction=SYSTEM_PROMPT,
                response_schema=RESPONSE_SCHEMA,
            ),
        ),
        attempts=3,
        base_delay=1.0,
        log=log,
        label="Gemini expense parser",
    )
    data = json.loads(resp.text)

    # Parse amount from LLM response using Decimal for precision
    amount_decimal = abs(Decimal(str(data.get("monto", data.get("amount", 0)) or 0)))
    amount_cents = int((amount_decimal * Decimal(100)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    
    description = (data.get("descripcion") or data.get("description") or "").strip() or "Movimiento"
    category = (data.get("categoria") or data.get("category_name") or "").strip()
    cats_map = {c.lower(): c for c in categories}
    if category and category.lower() in cats_map:
        category = cats_map[category.lower()]
    account = (data.get("cuenta") or "").strip()
    tx_type = (data.get("tipo") or "").strip().lower()
    if tx_type not in {"ingreso", "gasto", "transferencia"}:
        # Determine tx_type from amount sign using Decimal
        raw_amount = Decimal(str(data.get("amount") or 0))
        tx_type = "ingreso" if raw_amount > 0 else "gasto"

    account_dest = (data.get("cuenta_destino") or "").strip()

    if tx_type == "transferencia":
        signed_cents = amount_cents  # transferencias son positivas
    else:
        signed_cents = amount_cents if tx_type == "ingreso" else -amount_cents
        # Apply explicit sign override if present in raw text
        if signed_cents > 0 and text.strip().startswith('-'):
            signed_cents = -signed_cents
            tx_type = "gasto"
        elif signed_cents < 0 and text.strip().startswith('+'):
            signed_cents = abs(signed_cents)
            tx_type = "ingreso"

    dstr = (data.get("fecha") or data.get("date") or today.isoformat()).strip()[:10]
    try:
        datetime.strptime(dstr, "%Y-%m-%d")
    except ValueError:
        dstr = today.isoformat()

    return ParsedExpense(
        amount_cents=signed_cents,
        description=description,
        category=category,
        date=dstr,
        account=account,
        account_dest=account_dest,
        tx_type=tx_type,
        currency=default_currency.upper(),
        confidence=0.8,
        needs_confirmation=False,
    )


@dataclass
class LedgerRow:
    date: str
    description: str
    amount_cents: int            # Primary: integer cents (100 = $1.00)
    category: str = ""
    account: str = ""
    account_dest: str = ""
    tx_type: str = "gasto"
    currency: str = "ARS"
    source: str = "bot"          # "manual" | "bot"
    firefly_id: str = ""
    _row_index: int = -1         # id interno en SQLite
    tx_fingerprint: str = ""     # Unique constraint for duplicate prevention
    idempotency_key: str = ""    # For external API idempotency
    sync_status: str = "pending" # "pending" | "synced" | "failed"
    
    @property
    def amount(self) -> float:
        """Legacy accessor - returns float for backward compatibility (display only)."""
        return self.amount_cents / 100.0
    
    @property
    def amount_display(self) -> str:
        """Formatted amount for display."""
        from money_utils import cents_to_display
        return cents_to_display(self.amount_cents, self.currency)


@dataclass
class ImportBatch:
    id: int
    filename: str


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def _like_escape(term: str) -> str:
    """Escapa caracteres especiales de LIKE (%, _, \\) para que coincidan literalmente."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_FILLER = frozenset({
    "transferencia", "transferida", "recibida", "enviada",
    "pago", "compra", "venta", "cobro", "devolucion",
    "credito", "creditos", "debito", "cuota",
    "de", "del", "la", "el", "los", "las",
    "a", "al", "en", "por", "con", "para", "sin",
    "una", "uno", "un", "su", "mi",
})


def _descriptions_compatible(a: str, b: str) -> bool:
    """Return True if two descriptions are similar enough to be the same tx.

    Strips accents, lowercases, and ignores common financial filler words so
    that the comparison focuses on the distinguishing parts (names, stores).
    """
    a_norm = _strip_accents(a.strip().lower())
    b_norm = _strip_accents(b.strip().lower())
    if not a_norm or not b_norm:
        return True
    if a_norm == b_norm:
        return True
    if a_norm in b_norm or b_norm in a_norm:
        return True
    words_a = set(a_norm.split())
    words_b = set(b_norm.split())
    sig_a = words_a - _FILLER
    sig_b = words_b - _FILLER
    if not sig_a or not sig_b:
        return True
    only_a = sig_a - sig_b
    only_b = sig_b - sig_a
    if only_a and only_b:
        return False
    return True


def parse_asset_account_map(raw: str, *, default_asset_id: int) -> dict[str, int]:
    """Parsea 'Efectivo:1,Banco:2,MP:3' para resolver cuentas asset."""
    accounts: dict[str, int] = {"default": int(default_asset_id)}
    for item in (raw or "").split(","):
        if ":" not in item:
            continue
        name, value = item.split(":", 1)
        name = name.strip()
        try:
            account_id = int(value.strip())
        except ValueError:
            continue
        if name:
            accounts[name] = account_id
    return accounts


def _normalize_account_key(value: str) -> str:
    return re.sub(r"\s+", " ", _strip_accents(value).lower()).strip()


def resolve_asset_account_id(
    account_name: str,
    account_map: dict[str, int] | None,
    *,
    default_asset_id: int,
) -> int:
    """Mapea alias de cuenta a account_id de Firefly III con fallback seguro."""
    if not account_map:
        return int(default_asset_id)
    default_id = int(account_map.get("default", default_asset_id))
    needle = _normalize_account_key(account_name)
    if not needle:
        return default_id

    normalized = {
        _normalize_account_key(alias): int(account_id)
        for alias, account_id in account_map.items()
        if alias != "default"
    }
    if needle in normalized:
        return normalized[needle]
    for alias, account_id in normalized.items():
        if alias and (alias in needle or needle in alias):
            return account_id
    return default_id


# ---------------------------------------------------------------------------
# Migraciones de esquema SQLite
# ---------------------------------------------------------------------------
# Cada entrada: (version: int, description: str, sql_or_callable)
# La version 1 es el esquema inicial manejado por _init_db.
# Agregar aqui migraciones futuras (se aplican una sola vez en orden).
_MIGRATIONS: list[tuple[int, str, str]] = [
    (
        2,
        "index on ledger_entries.firefly_id for get_unsynced queries",
        "CREATE INDEX IF NOT EXISTS idx_ledger_firefly_id ON ledger_entries(firefly_id)",
    ),
    (
        3,
        "backfill amount_cents from legacy amount",
        "UPDATE ledger_entries SET amount_cents = CAST(ROUND(amount * 100) AS INTEGER) WHERE amount_cents IS NULL",
    ),
    (
        4,
        "placeholder - tx_fingerprint backfilled by Ledger._backfill_fingerprints()",
        "SELECT 1",
    ),
    (
        5,
        "placeholder - idempotency_key backfilled by Ledger._backfill_idempotency_keys()",
        "SELECT 1",
    ),
    (
        6,
        "placeholder - sync_status added by _ensure_column()",
        "SELECT 1",
    ),
    (
        7,
        "index on sync_status for efficient unsynced queries",
        "CREATE INDEX IF NOT EXISTS idx_ledger_sync_status ON ledger_entries(sync_status)",
    ),
]


def _apply_pending_migrations(conn: sqlite3.Connection) -> None:
    """Aplica migraciones pendientes registradas en _MIGRATIONS."""
    applied = {
        row[0]
        for row in conn.execute("SELECT version FROM schema_version").fetchall()
    }
    if 1 not in applied:
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, description) VALUES (1, 'initial schema')"
        )
    for version, description, sql in _MIGRATIONS:
        if version not in applied:
            log.info("Aplicando migracion DB v%d: %s", version, description)
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_version (version, description) VALUES (?, ?)",
                (version, description),
            )


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
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    description TEXT NOT NULL DEFAULT '',
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    description TEXT NOT NULL,
                    amount REAL NOT NULL,
                    category TEXT NOT NULL DEFAULT '',
                    account TEXT NOT NULL DEFAULT '',
                    currency TEXT NOT NULL DEFAULT 'ARS',
                    tx_type TEXT NOT NULL DEFAULT 'gasto',
                    source TEXT NOT NULL DEFAULT 'bot',
                    firefly_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._ensure_column(conn, "ledger_entries", "account", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "ledger_entries", "account_dest", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "ledger_entries", "currency", "TEXT NOT NULL DEFAULT 'ARS'")
            self._ensure_column(conn, "ledger_entries", "tx_type", "TEXT NOT NULL DEFAULT 'gasto'")
            # Migration: new columns for safe money handling
            self._ensure_column(conn, "ledger_entries", "amount_cents", "INTEGER")
            self._ensure_column(conn, "ledger_entries", "tx_fingerprint", "TEXT")
            self._ensure_column(conn, "ledger_entries", "idempotency_key", "TEXT")
            self._ensure_column(conn, "ledger_entries", "sync_status", "TEXT DEFAULT 'pending'")
            # Legacy index (kept for compatibility)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_ledger_amount_date
                ON ledger_entries(amount, date)
                """
            )
            # NEW: Unique index for duplicate prevention
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_fingerprint 
                ON ledger_entries(tx_fingerprint) WHERE tx_fingerprint IS NOT NULL
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_ledger_idempotency 
                ON ledger_entries(idempotency_key) WHERE idempotency_key IS NOT NULL
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger_operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_type TEXT NOT NULL,
                    ledger_entry_id INTEGER,
                    external_id TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    amount REAL,
                    status TEXT NOT NULL DEFAULT 'ok',
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_operations_created_at
                ON ledger_operations(created_at)
                """
            )
            self._ensure_column(conn, "ledger_operations", "chat_id", "INTEGER")
            self._ensure_column(conn, "ledger_operations", "user_id", "INTEGER")
            self._ensure_column(conn, "ledger_operations", "username", "TEXT NOT NULL DEFAULT ''")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS import_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    source_format TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'processing',
                    total INTEGER NOT NULL DEFAULT 0,
                    created INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    errors INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    finished_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS import_rows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    import_id INTEGER NOT NULL,
                    row_no INTEGER NOT NULL,
                    external_id TEXT NOT NULL DEFAULT '',
                    date TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    amount REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(import_id) REFERENCES import_batches(id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_import_rows_external_id
                ON import_rows(external_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS category_overrides (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pattern TEXT NOT NULL UNIQUE,
                    category TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            _apply_pending_migrations(conn)
            self._backfill_fingerprints(conn)
            self._backfill_idempotency_keys(conn)

    def _backfill_fingerprints(self, conn: sqlite3.Connection) -> None:
        """Backfill fingerprints using Python (ensures consistency with runtime)."""
        from money_utils import generate_fingerprint
        
        rows = conn.execute(
            "SELECT id, date, amount_cents, description FROM ledger_entries WHERE tx_fingerprint IS NULL OR tx_fingerprint = ''"
        ).fetchall()
        
        for row in rows:
            fingerprint = generate_fingerprint(
                row["date"] or "",
                row["amount_cents"] or 0,
                row["description"] or ""
            )
            conn.execute(
                "UPDATE ledger_entries SET tx_fingerprint = ? WHERE id = ?",
                (fingerprint, row["id"])
            )
        
        if rows:
            log.info("Backfilled fingerprints for %d rows", len(rows))

    def _backfill_idempotency_keys(self, conn: sqlite3.Connection) -> None:
        """Backfill idempotency keys using Python hashlib (SQLite lacks SHA256)."""
        from money_utils import generate_idempotency_key
        
        rows = conn.execute(
            "SELECT id, date, amount_cents, description FROM ledger_entries WHERE idempotency_key IS NULL OR idempotency_key = ''"
        ).fetchall()
        
        for row in rows:
            key = generate_idempotency_key(
                row["date"] or "",
                row["amount_cents"] or 0,
                row["description"] or ""
            )
            conn.execute(
                "UPDATE ledger_entries SET idempotency_key = ? WHERE id = ?",
                (key, row["id"])
            )
        
        if rows:
            log.info("Backfilled idempotency keys for %d rows", len(rows))

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        existing = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _row_to_ledger_row(row: sqlite3.Row) -> LedgerRow:
        # Prefer amount_cents, fallback to legacy amount column for migration
        amount_cents = row["amount_cents"]
        if amount_cents is None:
            # Backward compatibility: convert from legacy float
            amount_cents = int(round((row["amount"] or 0) * 100))
        
        tx_type = (row["tx_type"] or "").strip()
        if not tx_type:
            # Infer from sign
            tx_type = "ingreso" if amount_cents > 0 else "gasto"
        
        return LedgerRow(
            date=(row["date"] or "").strip(),
            description=(row["description"] or "").strip(),
            amount_cents=amount_cents,
            category=(row["category"] or "").strip(),
            account=(row["account"] or "").strip(),
            account_dest=(row["account_dest"] or "").strip(),
            currency=(row["currency"] or "ARS").strip().upper(),
            tx_type=tx_type,
            source=(row["source"] or "manual").strip(),
            firefly_id=(row["firefly_id"] or "").strip(),
            _row_index=int(row["id"]),
            tx_fingerprint=(row["tx_fingerprint"] or "").strip(),
            idempotency_key=(row["idempotency_key"] or "").strip(),
            sync_status=(row["sync_status"] or "pending").strip(),
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
                        amt_str = (row.get("amount") or "0").replace(",", ".")
                        amt_decimal = Decimal(amt_str)
                    except (ValueError, InvalidOperation):
                        amt_decimal = Decimal(0)
                    amt_cents = int((amt_decimal * Decimal(100)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
                    
                    date_str = (row.get("date") or "").strip()
                    desc_str = (row.get("description") or "").strip()
                    
                    # Generate fingerprint for migrated data
                    from money_utils import generate_fingerprint, generate_idempotency_key
                    fingerprint = generate_fingerprint(date_str, amt_cents, desc_str)
                    idempotency_key = generate_idempotency_key(date_str, amt_cents, desc_str)
                    
                    rows.append(
                        (
                            date_str,
                            desc_str,
                            float(amt_decimal),  # Legacy: float for backward compat
                            amt_cents,  # New: integer cents
                            (row.get("category") or "").strip(),
                            (row.get("account") or row.get("cuenta") or "").strip(),
                            (row.get("currency") or row.get("moneda") or "ARS").strip().upper(),
                            (row.get("tx_type") or row.get("tipo") or ("ingreso" if amt_decimal > 0 else "gasto")).strip(),
                            (row.get("source") or "manual").strip(),
                            (row.get("firefly_id") or "").strip(),
                            fingerprint,
                            idempotency_key,
                        )
                    )
            if rows:
                conn.executemany(
                    """
                    INSERT INTO ledger_entries
                    (date, description, amount, amount_cents, category, account, currency, 
                     tx_type, source, firefly_id, tx_fingerprint, idempotency_key)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                log.info("Migradas %d filas de %s a %s", len(rows), self.legacy_csv_path, self.path)

    def stats(self) -> dict[str, int]:
        with self._db() as conn:
            entry_count = conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0]
            unsynced = conn.execute(
                "SELECT COUNT(*) FROM ledger_entries WHERE sync_status IN ('pending', 'failed')"
            ).fetchone()[0]
            import_count = conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0]
            import_errors = conn.execute(
                "SELECT COUNT(*) FROM import_rows WHERE status = 'error'"
            ).fetchone()[0]
            return {
                "entries": int(entry_count),
                "unsynced": int(unsynced),
                "imports": int(import_count),
                "import_errors": int(import_errors),
            }

    def get_unsynced(self, limit: int = 50) -> list[LedgerRow]:
        """Retorna entradas pendientes de sync: sync_status is 'pending' or 'failed'."""
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT id, date, description, amount, amount_cents, category, account, 
                       account_dest, currency, tx_type, source, firefly_id,
                       tx_fingerprint, idempotency_key, sync_status
                FROM ledger_entries
                WHERE sync_status IN ('pending', 'failed')
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [self._row_to_ledger_row(row) for row in rows]

    def retry_sync(
        self,
        firefly: FireflyClient,
        *,
        asset_id: int,
        asset_accounts: dict[str, int] | None = None,
        currency: str = "ARS",
        limit: int = 50,
    ) -> tuple[int, int]:
        """Reintenta sincronizar con Firefly las entradas pending (solo 'pending', no 'failed').

        Solo reintenta entradas con sync_status='pending' para evitar duplicados
        en Firefly (una entrada 'failed' podria haber sido creada en Firefly
        pero la respuesta se perdio).

        Retorna (ok, failed).
        """
        # Solo reintentar entradas 'pending', no 'failed'
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT id, date, description, amount, amount_cents, category, account,
                       account_dest, currency, tx_type, source, firefly_id,
                       tx_fingerprint, idempotency_key, sync_status
                FROM ledger_entries
                WHERE sync_status = 'pending'
                ORDER BY id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            to_retry = [self._row_to_ledger_row(r) for r in rows]

        ok = 0
        failed = 0
        for row in to_retry:
            try:
                fid = _push_firefly(
                    row,
                    firefly=firefly,
                    asset_id=asset_id,
                    asset_accounts=asset_accounts,
                    currency=row.currency or currency,
                )
                self.update_row(row._row_index, firefly_id=fid, sync_status="synced")
                log.info("retry_sync ok: entry #%d -> firefly#%s", row._row_index, fid)
                ok += 1
            except FireflyError as e:
                self.update_row(row._row_index, sync_status="failed")
                log.error("retry_sync fallo para entry #%d: %s", row._row_index, e)
                failed += 1
        return ok, failed

    def recent_entries(self, limit: int = 5) -> list[LedgerRow]:
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT id, date, description, amount, amount_cents, category, account, 
                       account_dest, currency, tx_type, source, firefly_id,
                       tx_fingerprint, idempotency_key, sync_status
                FROM ledger_entries
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 20)),),
            ).fetchall()
        return [self._row_to_ledger_row(row) for row in rows]

    def search_entries(self, term: str, limit: int = 10) -> list[LedgerRow]:
        needle = f"%{_like_escape(term.strip())}%"
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT id, date, description, amount, amount_cents, category, account, 
                       account_dest, currency, tx_type, source, firefly_id,
                       tx_fingerprint, idempotency_key, sync_status
                FROM ledger_entries
                WHERE description LIKE ? ESCAPE '\\' OR category LIKE ? ESCAPE '\\' OR firefly_id LIKE ? ESCAPE '\\'
                ORDER BY id DESC
                LIMIT ?
                """,
                (needle, needle, needle, max(1, min(limit, 30))),
            ).fetchall()
        return [self._row_to_ledger_row(row) for row in rows]

    def search_import_rows(self, term: str, limit: int = 10) -> list[LedgerRow]:
        needle = f"%{_like_escape(term.strip())}%"
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT id, date, description, amount, external_id, status
                FROM import_rows
                WHERE description LIKE ? ESCAPE '\\' OR external_id LIKE ? ESCAPE '\\'
                ORDER BY id DESC
                LIMIT ?
                """,
                (needle, needle, max(1, min(limit, 30))),
            ).fetchall()
        return [
            LedgerRow(
                date=(row["date"] or "").strip(),
                description=(row["description"] or "").strip(),
                amount_cents=int(round(float(row["amount"] or 0) * 100)),  # Convert import to cents
                category=(row["status"] or "import").strip(),
                source="import",
                firefly_id=(row["external_id"] or "").strip(),
                _row_index=int(row["id"]),
            )
            for row in rows
        ]

    def create_import(self, filename: str, source_format: str = "") -> ImportBatch:
        with self._db() as conn:
            cur = conn.execute(
                """
                INSERT INTO import_batches (filename, source_format)
                VALUES (?, ?)
                """,
                (filename, source_format),
            )
            return ImportBatch(id=int(cur.lastrowid), filename=filename)

    def finish_import(self, import_id: int, *, total: int, created: int, skipped: int, errors: int) -> None:
        status = "error" if errors and not created else "partial" if errors else "ok"
        with self._db() as conn:
            conn.execute(
                """
                UPDATE import_batches
                SET status = ?, total = ?, created = ?, skipped = ?, errors = ?,
                    finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, total, created, skipped, errors, import_id),
            )

    def record_import_row(
        self,
        import_id: int,
        row_no: int,
        row: dict,
        *,
        status: str,
        error: str = "",
    ) -> None:
        amount = 0.0
        try:
            amount = float(row.get("Amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO import_rows
                (import_id, row_no, external_id, date, description, amount, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    import_id,
                    row_no,
                    (row.get("External_ID") or "").strip(),
                    (row.get("Date") or "").strip(),
                    (row.get("Description") or "").strip(),
                    amount,
                    status,
                    error[:500],
                ),
            )

    def record_operation(
        self,
        operation_type: str,
        *,
        row: LedgerRow | None = None,
        external_id: str = "",
        status: str = "ok",
        message: str = "",
        chat_id: int | None = None,
        user_id: int | None = None,
        username: str = "",
    ) -> None:
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO ledger_operations
                (operation_type, ledger_entry_id, external_id, description, amount, status, message,
                 chat_id, user_id, username)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_type,
                    row._row_index if row else None,
                    external_id,
                    row.description if row else "",
                    row.amount_cents if row else None,  # Log cents, not float
                    status,
                    message[:500],
                    chat_id,
                    user_id,
                    username[:50] if username else "",
                ),
            )

    def _read_all(self) -> list[LedgerRow]:
        out: list[LedgerRow] = []
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT id, date, description, amount, amount_cents, category, account, 
                       account_dest, currency, tx_type, source, firefly_id,
                       tx_fingerprint, idempotency_key
                FROM ledger_entries
                ORDER BY id
                """
            ).fetchall()
            for row in rows:
                out.append(self._row_to_ledger_row(row))
        return out

    def find_match(
        self,
        amount_cents: int,
        date_str: str,
        *,
        tolerance_days: int = 1,
        description: str = "",
    ) -> LedgerRow | None:
        """Find matching transaction by amount_cents, date, and description.
        
        Uses exact cents comparison - no floating point tolerance needed.
        """
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return None
        date_lo = (target - timedelta(days=tolerance_days)).isoformat()
        date_hi = (target + timedelta(days=tolerance_days)).isoformat()
        with self._db() as conn:
            # Use amount_cents with exact integer comparison
            rows = conn.execute(
                """
                SELECT id, date, description, amount, amount_cents, category, account, 
                       account_dest, currency, tx_type, source, firefly_id,
                       tx_fingerprint, idempotency_key
                FROM ledger_entries
                WHERE (amount_cents = ? OR (amount_cents IS NULL AND CAST(ROUND(amount * 100) AS INTEGER) = ?))
                  AND date BETWEEN ? AND ?
                ORDER BY date
                """,
                (amount_cents, amount_cents, date_lo, date_hi),
            ).fetchall()
        if not rows:
            return None
        candidates: list[tuple[int, LedgerRow]] = []
        for row in rows:
            r = self._row_to_ledger_row(row)
            try:
                rd = datetime.strptime(r.date, "%Y-%m-%d").date()
            except ValueError:
                continue
            candidates.append((abs((rd - target).days), r))
        if not candidates:
            return None
        if description:
            compatible = [
                (d, r) for d, r in candidates
                if _descriptions_compatible(description, r.description)
            ]
            if compatible:
                candidates = compatible
            else:
                return None
        candidates.sort(key=lambda t: t[0])
        return candidates[0][1]

    def append(self, row: LedgerRow) -> tuple[int, bool]:
        """Atomic insert with duplicate prevention via fingerprint.
        
        Returns: (row_id, was_inserted) - was_inserted is False if duplicate
        """
        from money_utils import generate_fingerprint, generate_idempotency_key
        
        # Generate fingerprint for duplicate detection
        fingerprint = generate_fingerprint(row.date, row.amount_cents, row.description)
        idempotency_key = generate_idempotency_key(row.date, row.amount_cents, row.description)
        
        with self._db() as conn:
            # Use INSERT OR IGNORE with fingerprint as unique constraint
            # This eliminates the race condition in check-then-insert
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO ledger_entries
                (date, description, amount, amount_cents, category, account, account_dest, 
                 currency, tx_type, source, firefly_id, tx_fingerprint, idempotency_key, sync_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.date,
                    row.description,
                    row.amount,  # Legacy: float for backward compatibility
                    row.amount_cents,
                    row.category,
                    row.account,
                    row.account_dest,
                    row.currency,
                    row.tx_type,
                    row.source,
                    row.firefly_id,
                    fingerprint,
                    idempotency_key,
                    row.sync_status,
                ),
            )
            
            if cur.lastrowid:
                # New row inserted
                row._row_index = int(cur.lastrowid)
                row.tx_fingerprint = fingerprint
                row.idempotency_key = idempotency_key
                return (row._row_index, True)
            else:
                # Duplicate - fetch existing row
                existing = conn.execute(
                    "SELECT id FROM ledger_entries WHERE tx_fingerprint = ?",
                    (fingerprint,)
                ).fetchone()
                if existing:
                    return (int(existing["id"]), False)
                # Should not happen, but return 0, False as fallback
                return (0, False)

    def update_row(self, row_index: int, **fields) -> None:
        allowed = {
            "date", "description", "amount", "amount_cents", "category", 
            "account", "account_dest", "currency", "tx_type", "source", 
            "firefly_id", "tx_fingerprint", "idempotency_key", "sync_status"
        }
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
                SELECT id, date, description, amount, amount_cents, category, account, 
                       account_dest, currency, tx_type, source, firefly_id,
                       tx_fingerprint, idempotency_key, sync_status
                FROM ledger_entries
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM ledger_entries WHERE id = ?", (row["id"],))
            return self._row_to_ledger_row(row)


@dataclass
class RecordResult:
    action: str           # "created" | "synced_from_csv" | "already_synced" | "noop"
    row: LedgerRow
    message: str = ""

    def summary(self) -> str:
        r = self.row
        if r.tx_type == "transferencia":
            sign = "=>"
        else:
            sign = "-" if r.amount_cents < 0 else "+"
        cat = r.category or "(sin categoria)"
        account = f"  cuenta={r.account}" if r.account else ""
        if r.account_dest:
            account += f" => {r.account_dest}"
        # Use cents_to_display for precise formatting
        from money_utils import cents_to_display
        amount_display = cents_to_display(r.amount_cents, r.currency)
        return (
            f"[{self.action}] {r.date}  {sign}{amount_display}  "
            f"{r.description}  [{cat}]"
            + account
            + (f"  firefly#{r.firefly_id}" if r.firefly_id else "")
            + (f"\n{self.message}" if self.message else "")
        )


def record_expense(
    parsed: ParsedExpense,
    *,
    ledger: Ledger,
    firefly: FireflyClient,
    asset_id: int,
    asset_accounts: dict[str, int] | None = None,
    currency: str = "ARS",
) -> RecordResult:
    if parsed.amount_cents == 0:
        return RecordResult(
            action="noop",
            row=LedgerRow(
                date=parsed.date,
                description=parsed.description,
                amount_cents=0,
                category=parsed.category,
                account=parsed.account,
                currency=parsed.currency,
                tx_type=parsed.tx_type,
            ),
            message="No se reconocio un monto.",
        )

    match = ledger.find_match(
        parsed.amount_cents,
        parsed.date,
        tolerance_days=0,
        description=parsed.description,
    )

    if match is not None:
        if not match.category and parsed.category:
            ledger.update_row(match._row_index, category=parsed.category)
            match.category = parsed.category
            if match.firefly_id:
                try:
                    firefly.update_transaction_category(match.firefly_id, parsed.category)
                except FireflyError as e:
                    log.error("Actualizar categoria en Firefly fallo (entry #%d): %s", match._row_index, e)
        if match.firefly_id:
            return RecordResult(
                action="already_synced",
                row=match,
                message="Ya existia en CSV y en Firefly.",
            )

        # Fecha/cuenta/tipo son metadata tecnica de sincronizacion; la
        # descripcion/categoria manual del ledger siguen siendo la verdad.
        updates: dict[str, str] = {}
        if match.date != parsed.date:
            updates["date"] = parsed.date
            match.date = parsed.date
        if parsed.account and match.account != parsed.account:
            updates["account"] = parsed.account
            match.account = parsed.account
        if parsed.account_dest and match.account_dest != parsed.account_dest:
            updates["account_dest"] = parsed.account_dest
            match.account_dest = parsed.account_dest
        if parsed.tx_type and match.tx_type != parsed.tx_type:
            updates["tx_type"] = parsed.tx_type
            match.tx_type = parsed.tx_type
        if parsed.currency and match.currency != parsed.currency:
            updates["currency"] = parsed.currency
            match.currency = parsed.currency
        if updates:
            ledger.update_row(match._row_index, **updates)

        # Existe en CSV pero no en Firefly. La descripcion/categoria del CSV ganan.
        try:
            fid = _push_firefly(
                match,
                firefly=firefly,
                asset_id=asset_id,
                asset_accounts=asset_accounts,
                currency=match.currency or currency,
            )
            ledger.update_row(match._row_index, firefly_id=fid, sync_status="synced")
            match.firefly_id = fid
            match.sync_status = "synced"
        except FireflyError as e:
            ledger.update_row(match._row_index, sync_status="failed")
            match.sync_status = "failed"
            log.error("Push a Firefly fallo (entry #%d csv, queda pendiente): %s", match._row_index, e)
            # Trigger small retry batch in background
            _trigger_background_retry(ledger, firefly, asset_id, asset_accounts, currency)
            return RecordResult(
                action="sync_failed",
                row=match,
                message=f"Entrada en ledger. Firefly no respondio: {e}. Reintento automatico programado.",
            )
        return RecordResult(
            action="synced_from_csv",
            row=match,
            message="Uso la version manual del CSV (no sobreescribo).",
        )

    # Nueva entrada — guardamos en ledger PRIMERO para no perder la operacion
    # Usamos INSERT OR IGNORE con fingerprint para prevencion atomica de duplicados
    new_row = LedgerRow(
        date=parsed.date,
        description=parsed.description,
        amount_cents=parsed.amount_cents,
        category=parsed.category,
        account=parsed.account,
        account_dest=parsed.account_dest,
        currency=parsed.currency,
        tx_type=parsed.tx_type,
        source="bot",
    )
    idx, was_inserted = ledger.append(new_row)
    
    if not was_inserted:
        # Duplicate detected atomically by database
        existing = ledger.find_match(
            parsed.amount_cents,
            parsed.date,
            tolerance_days=0,
            description=parsed.description,
        )
        if existing and existing.firefly_id:
            return RecordResult(
                action="already_synced",
                row=existing,
                message="Transaccion ya existia (detectado por fingerprint).",
            )
        # Update idx to the existing row for potential sync
        if existing:
            idx = existing._row_index
            new_row = existing
    
    new_row._row_index = idx
    
    try:
        fid = _push_firefly(
            new_row,
            firefly=firefly,
            asset_id=asset_id,
            asset_accounts=asset_accounts,
            currency=new_row.currency or currency,
        )
        new_row.firefly_id = fid
        new_row.sync_status = "synced"
        ledger.update_row(idx, firefly_id=fid, sync_status="synced")
    except FireflyError as e:
        new_row.sync_status = "pending"
        log.error("Push a Firefly fallo (entry #%d queda pendiente de sync): %s", idx, e)
        # Trigger small retry batch in background
        _trigger_background_retry(ledger, firefly, asset_id, asset_accounts, currency)
        return RecordResult(
            action="created_pending",
            row=new_row,
            message=f"Guardado localmente. Firefly no respondio: {e}. Reintento automatico en curso.",
        )
    
    # Mark as synced if we got here (Firefly push succeeded)
    new_row.sync_status = "synced"
    action = "created" if was_inserted else "synced_duplicate"
    return RecordResult(
        action=action,
        row=new_row,
        message="Agregado al ledger y a Firefly." if was_inserted else "Transaccion sincronizada (era duplicada).",
    )


def _firefly_transaction_date(row_date: str) -> str:
    try:
        parsed = datetime.strptime(row_date[:10], "%Y-%m-%d").date()
    except ValueError:
        return row_date
    now = datetime.now().astimezone()
    if parsed == now.date():
        return now.isoformat(timespec="seconds")
    return datetime.combine(parsed, datetime.min.time()).astimezone().isoformat(timespec="seconds")


def _push_firefly(
    row: LedgerRow,
    *,
    firefly: FireflyClient,
    asset_id: int,
    currency: str,
    asset_accounts: dict[str, int] | None = None,
) -> str:
    from money_utils import generate_idempotency_key
    
    is_withdrawal = row.amount_cents < 0
    account_id = resolve_asset_account_id(
        row.account,
        asset_accounts,
        default_asset_id=asset_id,
    )
    # Convert cents to decimal string for Firefly using Decimal (exact conversion)
    from decimal import Decimal, ROUND_HALF_UP
    amount_abs_cents = abs(row.amount_cents)
    amount_abs = str((Decimal(amount_abs_cents) / Decimal(100)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    desc = row.description or ("Gasto" if is_withdrawal else "Ingreso")
    tx_date = _firefly_transaction_date(row.date)
    
    # Generate idempotency key for external API call
    idempotency_key = row.idempotency_key or generate_idempotency_key(
        row.date, row.amount_cents, row.description
    )

    if row.tx_type == "transferencia":
        dest_id = resolve_asset_account_id(
            row.account_dest,
            asset_accounts,
            default_asset_id=asset_id,
        )
        tx: dict = {
            "type": "transfer",
            "date": tx_date,
            "amount": amount_abs,
            "currency_code": currency,
            "description": desc,
            "source_id": account_id,
            "destination_id": dest_id,
            "tags": ["telegram-bot", "nl"],
            "notes": "Registrado via Telegram bot (lenguaje natural).",
            "external_id": idempotency_key,  # Use as external_id for idempotency
        }
    else:
        tx: dict = {
            "type": "withdrawal" if is_withdrawal else "deposit",
            "date": tx_date,
            "amount": amount_abs,
            "currency_code": currency,
            "description": desc,
            "tags": ["telegram-bot", "nl"],
            "notes": "Registrado via Telegram bot (lenguaje natural).",
            "external_id": idempotency_key,  # Use as external_id for idempotency
        }
        if is_withdrawal:
            tx["source_id"] = account_id
            tx["destination_name"] = desc
        else:
            tx["source_name"] = desc
            tx["destination_id"] = account_id

    if row.category:
        tx["category_name"] = row.category

    payload = {
        "error_if_duplicate_hash": False,
        "apply_rules": True,
        "fire_webhooks": False,
        "transactions": [tx],
    }
    resp = firefly.create_transaction(payload)
    data = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(data, dict):
        fid = str(data.get("id") or "")
        if not fid:
            log.error(
                "Firefly respondio OK pero sin ID (transaccion creada). resp=%.300s", resp
            )
            return "sync_partial"
        return fid
    log.error(
        "Respuesta inesperada de Firefly al crear transaccion. resp=%.300s", resp
    )
    return "sync_partial"


# ---------------------------------------------------------------------------
# Background Sync Worker
# ---------------------------------------------------------------------------
_background_worker_running = False
_background_worker_lock = threading.Lock()

def _trigger_background_retry(
    ledger: Ledger,
    firefly: FireflyClient,
    asset_id: int,
    asset_accounts: dict[str, int] | None,
    currency: str,
) -> None:
    """Trigger a small retry batch in background thread."""
    def retry_task():
        try:
            time.sleep(2)  # Wait 2 seconds before retry
            ok, failed = ledger.retry_sync(
                firefly,
                asset_id=asset_id,
                asset_accounts=asset_accounts,
                currency=currency,
                limit=5,  # Small batch
            )
            if ok > 0 or failed > 0:
                log.info("Background retry completed: %d ok, %d failed", ok, failed)
        except Exception as e:
            log.error("Background retry failed: %s", e)
    
    thread = threading.Thread(target=retry_task, daemon=True)
    thread.start()


def start_background_sync_worker(
    ledger: Ledger,
    firefly: FireflyClient,
    asset_id: int,
    asset_accounts: dict[str, int] | None = None,
    currency: str = "ARS",
    interval_seconds: int = 300,  # 5 minutes
) -> None:
    """Start a background worker that retries unsynced entries every N seconds.
    
    Call this once at bot startup. Safe to call multiple times - it's a singleton.
    """
    global _background_worker_running
    
    with _background_worker_lock:
        if _background_worker_running:
            log.info("Background sync worker already running")
            return
        _background_worker_running = True
    
    def worker_loop():
        log.info("Background sync worker started (interval=%ds)", interval_seconds)
        while True:
            try:
                time.sleep(interval_seconds)
                stats = ledger.stats()
                if stats["unsynced"] > 0:
                    log.info("Background sync: found %d unsynced entries", stats["unsynced"])
                    ok, failed = ledger.retry_sync(
                        firefly,
                        asset_id=asset_id,
                        asset_accounts=asset_accounts,
                        currency=currency,
                        limit=20,
                    )
                    log.info("Background sync completed: %d ok, %d failed", ok, failed)
            except Exception as e:
                log.error("Background sync worker error: %s", e)
    
    thread = threading.Thread(target=worker_loop, daemon=True, name="sync_worker")
    thread.start()
    log.info("Background sync worker thread started")
