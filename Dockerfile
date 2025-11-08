FROM python:3.11-slim

# 基本ツールとフォント（Playwrightの日本語レンダリング用）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg \
    fonts-noto-cjk fonts-unifont \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt

# 依存インストール（PTB+JobQueue / Playwright+Chromium）
RUN pip install --no-cache-dir -r /app/requirements.txt && \
    python -m playwright install --with-deps chromium

# ソース配置
COPY bot.py /app/bot.py

ENV PYTHONUNBUFFERED=1
CMD ["python", "-u", "/app/bot.py"]
