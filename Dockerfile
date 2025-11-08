FROM python:3.11-slim

# 基本ツール
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt

# 依存インストール（job-queue付きPTBとPlaywright）
RUN pip install --no-cache-dir -r /app/requirements.txt && \
    python -m playwright install --with-deps chromium

# ソース配置
COPY bot.py /app/bot.py

ENV PYTHONUNBUFFERED=1
CMD ["python","-u","/app/bot.py"]
