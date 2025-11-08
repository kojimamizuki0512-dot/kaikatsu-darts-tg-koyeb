import os, re, json, asyncio, logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Set

import httpx
from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes,
)

# ====== 設定 ======
SHOP_URL = os.getenv("SHOP_URL", "https://www.kaikatsu.jp/shop/detail/vacancy.html?store_code=20328")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "120"))

# Jobの暴走防止（長すぎる処理は打ち切る）
HARD_TIMEOUT_SEC = 30

# ====== ロギング ======
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("kaikatsu-bot")

# ====== 状態 ======
subscribers: Set[int] = set()
last_status: Optional[str] = None

# ====== 取得・解析 ======
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/129.0.0.0 Safari/537.36",
    "Referer": "https://www.kaikatsu.jp/",
}

STATUS_RE = re.compile(r"ダーツ[^<]*?(満席|残\s*\d+\s*席)", re.S)

async def fetch_html(url: str) -> str:
    async with httpx.AsyncClient(http2=False, headers=HEADERS, timeout=httpx.Timeout(20.0)) as client:
        r = await client.get(url)
        r.raise_for_status()
        # サーバはUTF-8。明示して安全側に。
        r.encoding = "utf-8"
        return r.text

def parse_status(html: str) -> Optional[str]:
    m = STATUS_RE.search(html)
    if not m:
        return None
    text = m.group(1)
    # 正規化
    text = re.sub(r"\s+", "", text)
    return text  # 例: "満席" / "残1席"

async def get_shop_status() -> Optional[str]:
    html = await fetch_html(SHOP_URL)
    return parse_status(html)

# ====== コマンド ======
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "王子店『ダーツ』空席ウォッチです。\n"
        "/on で通知ON、/off で通知OFF、/status で現在の状況。/debug は解析用です。"
    )

async def cmd_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat:
        subscribers.add(update.effective_chat.id)
    await update.message.reply_text("通知を ON にしました。")

async def cmd_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat and update.effective_chat.id in subscribers:
        subscribers.remove(update.effective_chat.id)
    await update.message.reply_text("通知を OFF にしました。")

def jst_now_str() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        status = await asyncio.wait_for(get_shop_status(), timeout=HARD_TIMEOUT_SEC)
        if status:
            await update.message.reply_text(f"現在のダーツ: {status}（{jst_now_str()}）")
        else:
            await update.message.reply_text("取得に失敗しました。後でもう一度。")
    except Exception as e:
        log.exception("status error")
        await update.message.reply_text(f"取得エラー: {e}")

async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        html = await asyncio.wait_for(fetch_html(SHOP_URL), timeout=HARD_TIMEOUT_SEC)
        # ダーツ行周辺だけを抜粋して返す（長文防止）
        snippet = "…省略…"
        m = STATUS_RE.search(html)
        if m:
            start = max(m.start() - 60, 0)
            end = min(m.end() + 60, len(html))
            snippet = html[start:end]
        msg = f"status={last_status}\nURL={SHOP_URL}\n--- debug ---\n{snippet}"
        await update.message.reply_text(msg[:3500])
    except Exception as e:
        log.exception("debug error")
        await update.message.reply_text(f"debug error: {e}")

# ====== 定期ジョブ ======
async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    global last_status
    try:
        status = await asyncio.wait_for(get_shop_status(), timeout=HARD_TIMEOUT_SEC)
    except Exception as e:
        log.warning(f"poll error: {e}")
        return

    if not status:
        log.info("poll: status None")
        return

    if status != last_status:
        last_status = status
        log.info(f"status changed -> {status}")
        text = f"【更新】 王子店ダーツ: {status}（{jst_now_str()}）\n{SHOP_URL}"
        for chat_id in list(subscribers):
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
            except Exception as e:
                log.warning(f"send failed {chat_id}: {e}")

# ====== main ======
def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN が未設定です。")

    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("on",    cmd_on))
    app.add_handler(CommandHandler("off",   cmd_off))
    app.add_handler(CommandHandler("status",cmd_status))
    app.add_handler(CommandHandler("debug", cmd_debug))

    # 2分ごと。前回が終わらない場合はスキップ（溜めない）
    app.job_queue.run_repeating(
        poll_job,
        interval=CHECK_INTERVAL_SEC,
        first=5,
        job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 10},
        name="poll_job",
    )

    log.info("Bot starting…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
