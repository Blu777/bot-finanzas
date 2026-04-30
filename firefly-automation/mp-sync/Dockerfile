FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY firefly_client.py firefly_import.py telegram_bot.py seed_rules.py gemini_categorizer.py nl_expense.py ./

CMD ["python", "telegram_bot.py"]
