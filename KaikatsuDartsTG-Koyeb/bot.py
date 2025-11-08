import asyncio
import os
import re
from datetime import datetime, timezone, timedelta

import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes
)

# ====== 設定 ======
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
SHOP_URL = os.environ.get(
    "SHOP_URL",
    "https://www.kaikatsu.jp/shop/detail/vacancy.html?store_code=20328"
).strip()
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "120"))

JST = timezone(timedelta(hours=9))

# /on したチャットの集合（Koyeb は再起動で消える想定。必要ならDB等へ）
subs: set[int] = set()
last_status: str | None = None


def parse_status_from_html(html: str) -> str | None:
    """
    vacancy.html の HTML から「ダーツ」の現在の空席数を推定して文字列で返す。
    例: "残1席" / "満席" / "空席多数" 等。取れなければ None。
    """
    soup = BeautifulSoup(html, "lxml")

    # テーブルの th/td から「ダーツ」の行を探す
    # サイト側の表記ゆれ（全角スペースなど）を吸収
    for th in soup.find_all("th"):
        label = th.get_text(strip=True)
        if "ダーツ" in label:
            td = th.find_next("td")
            if not td:
                continue
            text = td.get_text(strip=True)

            # “残◯席”, “満席”, “空席” などを素直に返却
            m = re.search(r"(残\d+席|満席|空席|×|○|△)", text)
            return m.group(1) if m else text

    # fallback: 画面上の「現在のダーツ: 残1席」のような文言を拾う
    full = soup.get_text(" ", strip=True)
    m = re.search(r"現在のダーツ[:：]\s*([^\s　]+)", full)
    if m:
        return m.group(1)

    return None


async def fetch_status(client: httpx.AsyncClient) -> str | None:
    """HTTP/2は使わずに取得（h2依存を避ける）。"""
    r = await client.get(SHOP_URL, timeout=20)
    r.raise_for_status()
    return parse_status_from_html(r.text)


async def notify_all(app, text: str):
    for chat_id in list(subs):
        try:
            await app.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            # 送れなかったら購読から外すなどの処理を入れても良い
            pass


async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    global last_status
    async with httpx.AsyncClient(http2=False, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    }) as client:
        try:
            status = await fetch_status(client)
        except Exception as e:
            # サイレント失敗（ログだけ）
            print("poll error:", repr(e))
            return

    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    if status is None:
        print(f"[{now}] status=None")
        return

    if status != last_status:
        last_status = status
        msg = f"【更新】ダーツの空席状況: {status}（{now}）\n{SHOP_URL}"
        await notify_all(context.application, msg)
        print("notified:", msg)


# ====== Telegram コマンド ======
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "王子店『ダーツ』空席ウォッチです。\n"
        "/on で通知 ON、/off で通知 OFF、/status で現在の状況を取得します。"
    )

async def cmd_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subs.add(update.effective_chat.id)
    await update.message.reply_text("通知を ON にしました。")

async def cmd_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subs.discard(update.effective_chat.id)
    await update.message.reply_text("通知を OFF にしました。")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with httpx.AsyncClient(http2=False, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    }) as client:
        try:
            status = await fetch_status(client)
        except Exception:
            await update.message.reply_text("取得に失敗しました。後でもう一度。")
            return

    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    if status is None:
        await update.message.reply_text("取得に失敗しました。後でもう一度。")
    else:
        await update.message.reply_text(f"現在のダーツ: {status}（{now}）")


def ensure_env():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN が未設定です。KoyebのSecretsに設定して再デプロイしてください。")


async def main_async():
    ensure_env()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("on", cmd_on))
    app.add_handler(CommandHandler("off", cmd_off))
    app.add_handler(CommandHandler("status", cmd_status))

    # poll
    app.job_queue.run_repeating(poll_job, interval=CHECK_INTERVAL_SEC, first=5)

    print("Bot started")
    await app.run_polling(close_loop=False, drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main_async())
