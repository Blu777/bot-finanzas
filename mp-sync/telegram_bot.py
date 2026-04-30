"""Bot de Telegram que importa CSVs de Mercado Pago a Firefly III y aprende reglas.

Comandos:
- /start /help        - mensaje de ayuda
- /id                 - devuelve chat_id (util para autorizar)
- /categorias         - lista categorias en Firefly
- /reglas             - lista reglas creadas por el bot
- /aprender <kw> => <cat>  - crea regla "description contiene <kw> -> categoria <cat>"
- /borrar_regla <id>  - borra una regla por id
- (adjuntar CSV)      - importa el CSV
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from firefly_client import FireflyClient, FireflyError
from firefly_import import import_csv_file
from gemini_categorizer import categorize_pending
from nl_expense import Ledger, parse_expense, record_expense


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("mp-bot")


BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHATS = {
    int(x.strip()) for x in os.environ.get("TELEGRAM_ALLOWED_CHATS", "").split(",") if x.strip()
}
FIREFLY_URL = os.environ["FIREFLY_URL"]
FIREFLY_TOKEN = os.environ["FIREFLY_PERSONAL_TOKEN"]
ASSET_ID = int(os.environ["FIREFLY_ASSET_ACCOUNT_ID"])
CURRENCY = os.environ.get("CURRENCY", "ARS")
RULE_GROUP_TITLE = os.environ.get("RULE_GROUP_TITLE", "mp-bot")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
LOCAL_LEDGER_CSV = os.environ.get("LOCAL_LEDGER_CSV", "/data/ledger.csv")


client = FireflyClient(FIREFLY_URL, FIREFLY_TOKEN)
ledger = Ledger(LOCAL_LEDGER_CSV)


HELP = (
    "Comandos:\n"
    "  /start /help                       - este mensaje\n"
    "  /id                                - tu chat_id\n"
    "  /categorias                        - lista categorias\n"
    "  /reglas                            - lista reglas del bot\n"
    "  /aprender <palabra> => <categoria> - crea regla keyword->categoria\n"
    "  /borrar_regla <id>                 - borra regla por id\n"
    "  /categorizar                       - corre Gemini sobre tx pendientes\n"
    "  /aplicar_reglas                    - reaplica reglas a tx existentes\n"
    "  /deshacer                         - borra la ultima entrada del ledger y de Firefly\n"
    "\n"
    "Adjunta un CSV de Mercado Pago (Date,Description,Amount,External_ID) "
    "y lo importo a Firefly.\n"
    "\n"
    "Tambien podes escribir texto libre para registrar un gasto rapido:\n"
    "  '7000 chino'           -> Supermercado 7000 ARS hoy\n"
    "  'ayer 15 lucas nafta'  -> Transporte 15000 ARS ayer\n"
    "  '+50k sueldo'          -> Ingreso 50000 ARS hoy\n"
    "El bot primero consulta el CSV local (/data/ledger.csv). Si ya existe una "
    "entrada manual con el mismo monto y fecha, respeta la descripcion del CSV."
)


def _is_allowed(update: Update) -> bool:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not ALLOWED_CHATS:
        return False
    return chat_id in ALLOWED_CHATS


async def _guard(update: Update) -> bool:
    if _is_allowed(update):
        return True
    log.warning("Chat no autorizado: %s", update.effective_chat.id)
    await update.message.reply_text("Chat no autorizado.")
    return False


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await update.message.reply_text(HELP)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            )
        except ValueError as e:
            await msg.reply_text(f"CSV invalido: {e}")
            return
        except Exception as e:
            log.exception("Error importando")
            await msg.reply_text(f"Error inesperado: {e}")
            return

    summary = result.summary()
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

        parsed = await asyncio.to_thread(
            parse_expense,
            text,
            gemini_api_key=GEMINI_API_KEY,
            model=GEMINI_MODEL,
            categories=cats,
        )
        if parsed.amount == 0:
            await update.message.reply_text(
                "No detecte un monto. Ej: '7000 chino' o 'ayer 15k nafta'."
            )
            return

        result = await asyncio.to_thread(
            record_expense,
            parsed,
            ledger=ledger,
            firefly=client,
            asset_id=ASSET_ID,
            currency=CURRENCY,
        )
    except FireflyError as e:
        await update.message.reply_text(f"Firefly error: {e}")
        return
    except Exception as e:
        log.exception("NL expense fallo")
        await update.message.reply_text(f"Error: {e}")
        return

    await update.message.reply_text(f"```\n{result.summary()}\n```", parse_mode="Markdown")


async def cmd_deshacer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await update.message.reply_text("Deshaciendo ultima entrada...")
    removed = ledger.delete_last()
    if removed is None:
        await update.message.reply_text("No hay entradas en el ledger.")
        return
    parts = [f"Borrado del ledger: {removed.date} {removed.description} ${removed.amount:.2f}"]
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
    await update.message.reply_text("\n".join(parts))


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("categorias", cmd_categorias))
    app.add_handler(CommandHandler("reglas", cmd_reglas))
    app.add_handler(CommandHandler("aprender", cmd_aprender))
    app.add_handler(CommandHandler("borrar_regla", cmd_borrar_regla))
    app.add_handler(CommandHandler("categorizar", cmd_categorizar))
    app.add_handler(CommandHandler("aplicar_reglas", cmd_aplicar_reglas))
    app.add_handler(CommandHandler("deshacer", cmd_deshacer))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_other))

    log.info("Bot iniciado. Chats autorizados: %s", ALLOWED_CHATS or "(bloqueado - configurar ALLOWED_CHATS)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
