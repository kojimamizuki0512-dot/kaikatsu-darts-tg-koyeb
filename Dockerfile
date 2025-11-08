FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 依存に必要な最低限のツール
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates fonts-ipafont-gothic fonts-ipafont-mincho \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先に requirements を入れてキャッシュを効かせる
COPY KaikatsuDartsTG-Koyeb/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# アプリ本体
COPY KaikatsuDartsTG-Koyeb/bot.py /app/bot.py

# 起動
CMD ["python", "/app/bot.py"]
