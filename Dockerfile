# Playwright と Chromium が入った公式ベース
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

# 依存を入れる
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# アプリ本体
COPY bot.py /app/

# そのまま起動
CMD ["python", "bot.py"]
