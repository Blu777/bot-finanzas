FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY firefly_client.py firefly_import.py telegram_bot.py seed_rules.py gemini_categorizer.py nl_expense.py retry_utils.py gemini_config.py ./

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "from pathlib import Path; cmd=Path('/proc/1/cmdline').read_text(); raise SystemExit(0 if 'telegram_bot.py' in cmd else 1)"

CMD ["python", "telegram_bot.py"]
