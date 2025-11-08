# Playwright と Chromium 依存がプリインストール済みの公式イメージ
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# 依存（PTBのみ。playwrightはベースに同梱）
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# アプリ本体
COPY bot.py /app/bot.py

ENV PYTHONUNBUFFERED=1
CMD ["python", "-u", "/app/bot.py"]
