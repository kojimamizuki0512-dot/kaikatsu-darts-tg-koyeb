FROM python:3.11-slim

# ベース依存（証明書のみ）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY bot.py /app/bot.py

ENV PYTHONUNBUFFERED=1
CMD ["python", "-u", "/app/bot.py"]
