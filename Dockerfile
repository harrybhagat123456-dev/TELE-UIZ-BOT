# ── Quiz Practice Bot Dockerfile (Koyeb / Heroku / Docker) ───────────
FROM python:3.11-slim

WORKDIR /app

# Prevent bytecode & buffer stdout
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["python", "bot.py"]
