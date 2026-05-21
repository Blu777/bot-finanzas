"""Bot de Telegram que importa CSVs de Mercado Pago a Firefly III y aprende reglas.

Comandos:
- /start /help        - mensaje de ayuda
- /id                 - devuelve chat_id (util para autorizar)
- /categorias         - lista categorias en Firefly
- /reglas             - lista reglas creadas por el bot
- /aprender <kw> => <cat>  - crea regla "description contiene <kw> -> categoria <cat>"
- /borrar_regla <id>  - borra una regla por id
- /estado             - muestra salud/configuracion basica
- /ultimos [n]        - ultimos gastos del ledger
- /buscar <texto>     - busca en el ledger local
- (adjuntar CSV)      - importa el CSV
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path

import dataclasses

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Settings
from firefly_client import FireflyClient, FireflyError
from firefly_import import import_csv_file
from gemini_config import DEFAULT_GEMINI_MODEL
from gemini_categorizer import categorize_pending
from nl_expense import Ledger, parse_asset_account_map, parse_expenses, record_expense


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("mp-bot")


BOT_VERSION = "1.4"

_cfg = Settings.from_env()
BOT_TOKEN = _cfg.bot_token
ALLOWED_CHATS = _cfg.allowed_chats
FIREFLY_URL = _cfg.firefly_url
FIREFLY_TOKEN = _cfg.firefly_token
ASSET_ID = _cfg.asset_id
ASSET_ACCOUNTS = _cfg.asset_accounts
CURRENCY = _cfg.currency
RULE_GROUP_TITLE = _cfg.rule_group_title
GEMINI_API_KEY = _cfg.gemini_api_key
GEMINI_MODEL = _cfg.gemini_model or DEFAULT_GEMINI_MODEL
LOCAL_LEDGER_CSV = _cfg.local_ledger_csv


client = FireflyClient(FIREFLY_URL, FIREFLY_TOKEN)
ledger = Ledger(LOCAL_LEDGER_CSV)


def _validate_asset_accounts():
    ids = {ASSET_ID}
    ids.update(ASSET_ACCOUNTS.values())
    for aid in ids:
        acc = client.get_account(aid)
        if not acc:
            raise RuntimeError(
                f"Cuenta asset ID {aid} no existe en Firefly. "
                "Revisa FIREFLY_ASSET_ACCOUNT_ID y FIREFLY_ASSET_ACCOUNTS."
            )
        if acc["attributes"]["type"] != "asset":
            raise RuntimeError(
                f"Cuenta ID {aid} en Firefly es tipo '{acc['attributes']['type']}', "
                f"no 'asset'. Revisa FIREFLY_ASSET_ACCOUNT_ID."
            )


_validate_asset_accounts()


HELP = (
    "Comandos:\n"
    "  /start /help                       - este mensaje\n"
    "  /version                           - version del bot\n"
    "  /id                                - tu chat_id\n"
    "  /categorias                        - lista categorias\n"
    "  /reglas                            - lista reglas del bot\n"
    "  /aprender <palabra> => <categoria> - crea regla keyword->categoria\n"
    "  /borrar_regla <id>                 - borra regla por id\n"
    "  /categorizar                       - corre Gemini sobre tx pendientes\n"
    "  /aplicar_reglas                    - reaplica reglas a tx existentes\n"
    "  /deshacer                         - borra la ultima entrada del ledger y de Firefly\n"
    "  /estado                           - salud basica del bot\n"
    "  /ultimos [n]                      - ultimas entradas del ledger local\n"
    "  /buscar <texto>                   - busca en el ledger local\n"
    "  /retry                            - reintenta sync de entradas pendientes con Firefly\n"
    "\n"
    "Adjunta un CSV de Mercado Pago (statement o Date,Description,Amount,External_ID) "
    "y lo importo a Firefly.\n"
    "\n"
    "Tambien podes escribir texto libre para registrar un gasto rapido:\n"
    "  '7000 chino'           -> Supermercado 7000 ARS hoy\n"
    "  'ayer 15 lucas nafta'  -> Transporte 15000 ARS ayer\n"
    "  '+50k sueldo'          -> Ingreso 50000 ARS hoy\n"
    "El bot primero consulta el ledger SQLite local. Si ya existe una "
    "entrada manual con el mismo monto y fecha, respeta esa descripcion."
)


def _format_ledger_rows(rows) -> str:
    from money_utils import cents_to_display
    if not rows:
        return "Sin resultados."
    lines = []
    for r in rows:
        sign = "-" if r.amount_cents < 0 else "+"
        cat = r.category or "sin categoria"
        firefly = f" ff#{r.firefly_id}" if r.firefly_id else ""
        amount_display = cents_to_display(abs(r.amount_cents), r.currency).lstrip('$').lstrip('US$')
        lines.append(
            f"#{r._row_index} {r.date} {sign}${amount_display} "
            f"{r.description} [{cat}]{firefly}"
        )
    return "\n".join(lines)


_last_call: dict[int, float] = {}
_RATE_LIMIT_S = 1.0


def _is_allowed(update: Update) -> bool:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not ALLOWED_CHATS:
        return False
    return chat_id in ALLOWED_CHATS


async def _guard(update: Update) -> bool:
    chat = update.effective_chat
    if not _is_allowed(update):
        log.warning("Chat no autorizado: %s", chat.id if chat else "?")
        if update.message:
            await update.message.reply_text("Chat no autorizado.")
        return False
    if chat:
        now = time.monotonic()
        if now - _last_call.get(chat.id, 0.0) < _RATE_LIMIT_S:
            if update.message:
                await update.message.reply_text("Muchos mensajes seguidos, espera un momento.")
            return False
        _last_call[chat.id] = now
    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await update.message.reply_text(HELP)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    chat = update.effective_chat
    user = update.effective_user
    await update.message.reply_text(
        f"chat_id: {chat.id}\n"
        f"user_id: {user.id if user else 'n/a'}\n"
        f"username: @{user.username if user and user.username else ''}"
    )


async def cmd_categorias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    try:
        cats = await asyncio.to_thread(client.list_categories)
    except FireflyError as e:
        await update.message.reply_text(f"Error: {e}")
        return
    if not cats:
        await update.message.reply_text("No hay categorias todavia.")
        return
    lines = [f"- {c['attributes']['name']}" for c in cats]
    await update.message.reply_text("Categorias:\n" + "\n".join(lines))


async def cmd_reglas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    try:
        group = await asyncio.to_thread(client.get_or_create_rule_group, RULE_GROUP_TITLE)
        rules = await asyncio.to_thread(client.list_rules, group["id"])
    except FireflyError as e:
        await update.message.reply_text(f"Error: {e}")
        return
    if not rules:
        await update.message.reply_text(f"Grupo '{RULE_GROUP_TITLE}' sin reglas.")
        return
    lines = [f"#{r['id']}  {r['attributes']['title']}" for r in rules]
    await update.message.reply_text(
        f"Reglas en '{RULE_GROUP_TITLE}':\n" + "\n".join(lines)
    )


async def cmd_aprender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    text = (update.message.text or "").removeprefix("/aprender").strip()
    if "=>" not in text:
        await update.message.reply_text(
            "Uso:  /aprender <palabra clave> => <Categoria>\n"
            "Ej:   /aprender carrefour => Supermercado"
        )
        return
    kw_part, cat_part = text.split("=>", 1)
    keyword = kw_part.strip()
    category = cat_part.strip()
    if not keyword or not category:
        await update.message.reply_text("Palabra clave y categoria no pueden estar vacias.")
        return

    try:
        await asyncio.to_thread(client.get_or_create_category, category)
        group = await asyncio.to_thread(client.get_or_create_rule_group, RULE_GROUP_TITLE)
        existing = await asyncio.to_thread(
            client.find_rule_by_title, group["id"], f"{keyword} -> {category}"
        )
        if existing:
            await update.message.reply_text(
                f"Ya existe la regla #{existing['id']}: {existing['attributes']['title']}"
            )
            return
        rule = await asyncio.to_thread(
            client.create_keyword_to_category_rule, group["id"], keyword, category
        )
    except FireflyError as e:
        await update.message.reply_text(f"Error creando regla: {e}")
        return

    await update.message.reply_text(
        f"OK. Regla #{rule['id']} creada: '{keyword}' -> '{category}'\n"
        "Se aplicara automaticamente en proximos imports."
    )


async def cmd_borrar_regla(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Uso:  /borrar_regla <id>")
        return
    rid = args[0]
    if not rid.isdigit():
        await update.message.reply_text("Id invalido.")
        return
    try:
        await asyncio.to_thread(client.delete_rule, int(rid))
    except FireflyError as e:
        await update.message.reply_text(f"Error: {e}")
        return
    await update.message.reply_text(f"Regla #{rid} borrada.")


async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    stats = await asyncio.to_thread(ledger.stats)
    firefly_ok = "OK"
    try:
        await asyncio.to_thread(client.list_categories)
    except Exception as e:
        firefly_ok = f"ERROR: {e}"

    lines = [
        "Estado:",
        f"- Firefly: {firefly_ok}",
        f"- Gemini: {'configurado' if GEMINI_API_KEY else 'sin GEMINI_API_KEY'}",
        f"- Ledger: {ledger.path}",
        f"- Entradas NL/manuales: {stats['entries']}",
        f"- Pendientes de sync: {stats['unsynced']}",
        f"- Imports registrados: {stats['imports']}",
        f"- Filas de import con error: {stats['import_errors']}",
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_ultimos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    limit = 5
    if context.args:
        try:
            limit = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Uso: /ultimos [cantidad]")
            return
    rows = await asyncio.to_thread(ledger.recent_entries, limit)
    await update.message.reply_text(_format_ledger_rows(rows))


async def cmd_buscar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    term = " ".join(context.args or []).strip()
    if not term:
        await update.message.reply_text("Uso: /buscar <texto>")
        return
    rows = await asyncio.to_thread(ledger.search_entries, term, 10)
    if len(rows) < 10:
        rows.extend(await asyncio.to_thread(ledger.search_import_rows, term, 10 - len(rows)))
    await update.message.reply_text(_format_ledger_rows(rows))


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return

    msg = update.message
    doc = msg.document
    if not doc:
        return

    name = doc.file_name or "archivo"
    if not name.lower().endswith(".csv"):
        await msg.reply_text(f"`{name}` no es un .csv.", parse_mode="Markdown")
        return

    await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
    await msg.reply_text(f"Recibi `{name}`. Procesando...", parse_mode="Markdown")

    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / name
        f = await doc.get_file()
        await f.download_to_drive(custom_path=str(target))
        log.info("CSV %s (%.1f KB)", target, target.stat().st_size / 1024)

        try:
            result = await asyncio.to_thread(
                import_csv_file,
                str(target),
                client=client,
                asset_id=ASSET_ID,
                currency=CURRENCY,
                ledger=ledger,
            )
        except ValueError as e:
            await msg.reply_text(f"CSV invalido: {e}")
            return
        except Exception as e:
            log.exception("Error importando")
            await msg.reply_text(f"Error inesperado: {e}")
            return

    summary = result.summary()
    await asyncio.to_thread(
        ledger.record_operation,
        "import_csv",
        status="ok" if result.errors == 0 else "partial",
        message=summary,
        chat_id=msg.chat_id,
        user_id=update.effective_user.id if update.effective_user else None,
        username=(update.effective_user.username or "") if update.effective_user else "",
    )
    log.info("Resultado:\n%s", summary)
    await msg.reply_text(f"```\n{summary}\n```", parse_mode="Markdown")

    if GEMINI_API_KEY and result.created > 0:
        await msg.reply_text("Corriendo IA sobre las pendientes...")
        try:
            ai = await asyncio.to_thread(
                categorize_pending,
                client,
                GEMINI_API_KEY,
                model=GEMINI_MODEL,
            )
        except Exception as e:
            log.exception("Gemini fallo")
            await msg.reply_text(f"Gemini error: {e}")
            return
        await msg.reply_text(f"```\n{ai.summary()}\n```", parse_mode="Markdown")


async def cmd_categorizar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    if not GEMINI_API_KEY:
        await update.message.reply_text("GEMINI_API_KEY no configurada.")
        return
    await update.message.reply_text("Buscando pendientes y corriendo IA...")
    try:
        ai = await asyncio.to_thread(
            categorize_pending, client, GEMINI_API_KEY, model=GEMINI_MODEL
        )
    except Exception as e:
        log.exception("Gemini fallo")
        await update.message.reply_text(f"Error: {e}")
        return
    await update.message.reply_text(f"```\n{ai.summary()}\n```", parse_mode="Markdown")


async def cmd_aplicar_reglas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await update.message.reply_text(
        f"Disparando grupo de reglas '{RULE_GROUP_TITLE}' sobre tx existentes..."
    )
    try:
        group = await asyncio.to_thread(client.get_or_create_rule_group, RULE_GROUP_TITLE)
        await asyncio.to_thread(client.trigger_rule_group, group["id"])
    except FireflyError as e:
        await update.message.reply_text(f"Error: {e}")
        return
    except Exception as e:
        log.exception("trigger fallo")
        await update.message.reply_text(f"Error inesperado: {e}")
        return
    await update.message.reply_text(
        "OK. Reglas reaplicadas. Reviza /categorias y las tx en Firefly."
    )


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Texto libre -> extraer gasto via LLM + sync con ledger + Firefly."""
    if not await _guard(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    if not GEMINI_API_KEY:
        await update.message.reply_text(
            "GEMINI_API_KEY no configurada. Usa /help para ver comandos."
        )
        return

    await update.message.reply_text("Sumando cosas...")
    await context.bot.send_chat_action(
        chat_id=update.message.chat_id, action=ChatAction.TYPING
    )

    try:
        cats_raw = await asyncio.to_thread(client.list_categories)
        cats = [c["attributes"]["name"] for c in cats_raw]
        if not cats:
            await update.message.reply_text(
                "No hay categorias en Firefly. Corre /aprender o el seeder primero."
            )
            return

        parsed_result = await asyncio.to_thread(
            parse_expenses,
            text,
            gemini_api_key=GEMINI_API_KEY,
            model=GEMINI_MODEL,
            categories=cats,
            account_aliases=[a for a in ASSET_ACCOUNTS if a != "default"],
            default_currency=CURRENCY,
        )
        if not parsed_result.transactions or all(p.amount_cents == 0 for p in parsed_result.transactions):
            await update.message.reply_text(
                "No detecte un monto. Ej: '7000 chino' o 'ayer 15k nafta'."
            )
            return
        if parsed_result.needs_confirmation:
            context.chat_data["pending_nl_confirm"] = {
                "transactions": parsed_result.transactions,
            }
            preview = _format_parse_preview(parsed_result.transactions, parsed_result.warnings)
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Confirmar", callback_data="nlconfirm:ok"),
                        InlineKeyboardButton("Cancelar", callback_data="nlconfirm:cancel"),
                    ]
                ]
            )
            await update.message.reply_text(
                "Necesito confirmacion antes de guardar:\n"
                f"```\n{preview}\n```",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            return

        parsed = parsed_result.transactions[0]
        if len(parsed_result.transactions) > 1:
            results = []
            for item in parsed_result.transactions:
                result = await _do_record(item)
                results.append(result)
                await _record_operation_for_update(update, result)
            await update.message.reply_text(
                "```\n" + "\n\n".join(r.summary() for r in results) + "\n```",
                parse_mode="Markdown",
            )
            return

        cat_exists = parsed.category and any(
            c["attributes"]["name"].lower() == parsed.category.lower()
            for c in cats_raw
        )
        if parsed.category and not cat_exists:
            context.chat_data["pending_nl"] = {
                "parsed": parsed,
            }
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            f"Crear '{parsed.category}'",
                            callback_data="nlcat:create",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "Sin categoria",
                            callback_data="nlcat:skip",
                        )
                    ],
                ]
            )
            await update.message.reply_text(
                f"La categoria '{parsed.category}' no existe en Firefly.\n"
                "Queres crearla antes de guardar el gasto?",
                reply_markup=keyboard,
            )
            return

        result = await _do_record(parsed)
        await _record_operation_for_update(update, result)
    except FireflyError as e:
        await update.message.reply_text(f"Firefly error: {e}")
        return
    except Exception as e:
        log.exception("NL expense fallo")
        await update.message.reply_text(f"Error: {e}")
        return

    await update.message.reply_text(f"```\n{result.summary()}\n```", parse_mode="Markdown")


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await update.message.reply_text(f"mp-sync v{BOT_VERSION}")


async def cmd_deshacer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await update.message.reply_text("Deshaciendo ultima entrada...")
    removed = ledger.delete_last()
    if removed is None:
        await update.message.reply_text("No hay entradas en el ledger.")
        return
    from money_utils import cents_to_display
    amount_display = cents_to_display(removed.amount_cents, removed.currency)
    parts = [f"Borrado del ledger: {removed.date} {removed.description} {amount_display}"]
    if removed.firefly_id:
        try:
            await asyncio.to_thread(client.delete_transaction, int(removed.firefly_id))
            parts.append(f"Borrado de Firefly: tx #{removed.firefly_id}")
        except FireflyError as e:
            parts.append(f"Firefly error borrando tx #{removed.firefly_id}: {e}")
        except Exception as e:
            log.exception("Error borrando tx %s", removed.firefly_id)
            parts.append(f"Error: {e}")
    else:
        parts.append("No estaba sincronizado con Firefly.")
    await asyncio.to_thread(
        ledger.record_operation,
        "undo",
        row=removed,
        status="ok",
        message="\n".join(parts),
        chat_id=update.effective_chat.id if update.effective_chat else None,
        user_id=update.effective_user.id if update.effective_user else None,
        username=(update.effective_user.username or "") if update.effective_user else "",
    )
    await update.message.reply_text("\n".join(parts))


async def cmd_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    pending = await asyncio.to_thread(ledger.get_unsynced)
    if not pending:
        await update.message.reply_text("No hay entradas pendientes de sync con Firefly.")
        return
    await update.message.reply_text(f"Reintentando sync de {len(pending)} entrada(s)...")
    ok, failed = await asyncio.to_thread(
        ledger.retry_sync,
        client,
        asset_id=ASSET_ID,
        asset_accounts=ASSET_ACCOUNTS,
        currency=CURRENCY,
    )
    parts = ["Retry completado:", f"  Sincronizadas: {ok}"]
    if failed:
        parts.append(f"  Fallidas (Firefly no respondio): {failed}")
    await asyncio.to_thread(
        ledger.record_operation,
        "retry_sync",
        status="ok" if not failed else "partial",
        message=f"ok={ok} failed={failed}",
        chat_id=update.effective_chat.id if update.effective_chat else None,
        user_id=update.effective_user.id if update.effective_user else None,
        username=(update.effective_user.username or "") if update.effective_user else "",
    )
    await update.message.reply_text("\n".join(parts))


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CommandHandler("categorias", cmd_categorias))
    app.add_handler(CommandHandler("reglas", cmd_reglas))
    app.add_handler(CommandHandler("aprender", cmd_aprender))
    app.add_handler(CommandHandler("borrar_regla", cmd_borrar_regla))
    app.add_handler(CommandHandler("categorizar", cmd_categorizar))
    app.add_handler(CommandHandler("aplicar_reglas", cmd_aplicar_reglas))
    app.add_handler(CommandHandler("deshacer", cmd_deshacer))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CommandHandler("ultimos", cmd_ultimos))
    app.add_handler(CommandHandler("buscar", cmd_buscar))
    app.add_handler(CommandHandler("retry", cmd_retry))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_other))
    app.add_handler(CallbackQueryHandler(on_nl_confirm, pattern=r"^nlconfirm:"))
    app.add_handler(CallbackQueryHandler(on_cat_confirm, pattern=r"^nlcat:"))

    log.info("Bot iniciado. Chats autorizados: %s", ALLOWED_CHATS or "(bloqueado - configurar ALLOWED_CHATS)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


async def on_cat_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    pending = context.chat_data.pop("pending_nl", None)
    if not pending:
        await query.edit_message_text("Sesion expirada. Manda el gasto de nuevo.")
        return
    parsed = pending["parsed"]
    if data == "nlcat:create":
        try:
            await asyncio.to_thread(client.get_or_create_category, parsed.category)
        except Exception as e:
            log.exception("Error creando categoria")
            await query.edit_message_text(f"Error creando categoria: {e}")
            return
    elif data == "nlcat:skip":
        parsed = dataclasses.replace(parsed, category="")
    else:
        await query.edit_message_text("Opcion no reconocida.")
        return

    try:
        result = await _do_record(parsed)
        await asyncio.to_thread(
            ledger.record_operation,
            result.action,
            row=result.row,
            status="ok",
            message=result.message,
            chat_id=query.message.chat_id if query.message else None,
            user_id=query.from_user.id if query.from_user else None,
            username=(query.from_user.username or "") if query.from_user else "",
        )
    except FireflyError as e:
        await query.edit_message_text(f"Firefly error: {e}")
        return
    except Exception as e:
        log.exception("NL expense fallo")
        await query.edit_message_text(f"Error: {e}")
        return

    await query.edit_message_text(f"```\n{result.summary()}\n```", parse_mode="Markdown")


async def on_nl_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    pending = context.chat_data.pop("pending_nl_confirm", None)
    if not pending:
        await query.edit_message_text("Sesion expirada. Manda el gasto de nuevo.")
        return
    if data == "nlconfirm:cancel":
        await query.edit_message_text("Cancelado. No guarde nada.")
        return
    if data != "nlconfirm:ok":
        await query.edit_message_text("Opcion no reconocida.")
        return
    try:
        results = []
        for parsed in pending["transactions"]:
            result = await _do_record(parsed)
            results.append(result)
            await asyncio.to_thread(
                ledger.record_operation,
                result.action,
                row=result.row,
                status="ok",
                message=result.message,
                chat_id=query.message.chat_id if query.message else None,
                user_id=query.from_user.id if query.from_user else None,
                username=(query.from_user.username or "") if query.from_user else "",
            )
    except FireflyError as e:
        await query.edit_message_text(f"Firefly error: {e}")
        return
    except Exception as e:
        log.exception("NL confirm fallo")
        await query.edit_message_text(f"Error: {e}")
        return
    await query.edit_message_text(
        "```\n" + "\n\n".join(r.summary() for r in results) + "\n```",
        parse_mode="Markdown",
    )


async def _do_record(parsed) -> "RecordResult":
    return await asyncio.to_thread(
        record_expense,
        parsed,
        ledger=ledger,
        firefly=client,
        asset_id=ASSET_ID,
        asset_accounts=ASSET_ACCOUNTS,
        currency=CURRENCY,
    )


async def _record_operation_for_update(update: Update, result) -> None:
    await asyncio.to_thread(
        ledger.record_operation,
        result.action,
        row=result.row,
        status="ok",
        message=result.message,
        chat_id=update.effective_chat.id if update.effective_chat else None,
        user_id=update.effective_user.id if update.effective_user else None,
        username=(update.effective_user.username or "") if update.effective_user else "",
    )


def _format_parse_preview(transactions, warnings: list[str]) -> str:
    from money_utils import cents_to_display
    lines = []
    for i, item in enumerate(transactions, 1):
        direction = "=>" if item.tx_type == "transferencia" else "+" if item.amount_cents > 0 else "-"
        cat = item.category or "(sin categoria)"
        accounts = f" {item.account}" if item.account else ""
        if item.account_dest:
            accounts += f" => {item.account_dest}"
        amount_display = cents_to_display(abs(item.amount_cents), item.currency)
        lines.append(
            f"{i}. {item.date} {direction}{amount_display} "
            f"{item.description} [{cat}]{accounts}"
        )
        for warning in item.warnings:
            lines.append(f"   ! {warning}")
    seen = {w for tx in transactions for w in tx.warnings}
    for warning in warnings:
        if warning not in seen:
            lines.append(f"! {warning}")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
