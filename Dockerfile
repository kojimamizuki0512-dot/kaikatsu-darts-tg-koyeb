# Playwright + Python の公式イメージ（ブラウザ/依存込み）
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

# 作業ディレクトリ
WORKDIR /app

# 依存（PTBのみ。Playwrightはイメージに入っている）
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# ソース
COPY bot.py /app/bot.py

# ログを即時出力
ENV PYTHONUNBUFFERED=1

# 実行
CMD ["python", "-u", "/app/bot.py"]
