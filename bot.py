import os
import re
import json
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
)

from playwright.async_api import async_playwright

# ====== 設定 ======
SHOP_URL = os.environ.get(
    "SHOP_URL",
    "https://www.kaikatsu.jp/shop/detail/vacancy.html?store_code=20328",
)
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "120"))

JST = timezone(timedelta(hours=9))
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)

# メモリ保持（Koyeb再起動でリセットされます）
SUBSCRIBERS_PATH = "subs.json"
STATE_PATH = "state.json"
_subs = set()
_last_status = None
_fetch_lock = asyncio.Lock()


# ====== 永続もどき（JSON） ======
def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, obj):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning("save_json failed: %s", e)


def load_state():
    global _subs, _last_status
    _subs = set(_load_json(SUBSCRIBERS_PATH, []))
    st = _load_json(STATE_PATH, {"last_status": None})
    _last_status = st.get("last_status")


def save_state():
    _save_json(SUBSCRIBERS_PATH, list(_subs))
    _save_json(STATE_PATH, {"last_status": _last_status})


# ====== 解析 ======
def parse_darts_status(text: str) -> str | None:
    """
    本文テキストから「満席」 or 「残{n}席」を抽出。
    空白は吸収して正規化して返す（例: '残1席'）
    """
    if not text:
        return None

    # まずは「ダーツ … (満席|残n席)」パターン
    m = re.search(r"ダーツ.*?(満席|残\s*\d+\s*席)", text, re.S)
    if m:
        return re.sub(r"\s+", "", m.group(1))

    # 代替: 「ビリヤード/ダーツ」の行で個別数値が出るケース等もある
    # （必要に応じてここに別パターンを追加）
    return None


async def fetch_status_via_playwright() -> tuple[str | None, str]:
    """
    Playwrightでページをレンダリングして本文からダーツの状態を抜く。
    戻り値: (status, debug_snippet)
    """
    debug_snippet = ""
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(locale="ja-JP")
            page = await context.new_page()

            # ネットワーク静止まで待機（JS後レンダリングを待つ）
            await page.goto(SHOP_URL, wait_until="networkidle", timeout=60_000)

            # ページ全体の可視テキストを取得
            body_text = await page.inner_text("body")
            status = parse_darts_status(body_text)

            # 見つからなかった場合、ネットワークレスポンスからvacancy系も拾ってみる
            if status is None:
                # 直近のHTMLを短縮してデバッグ返却用に保持
                html = await page.content()
                debug_snippet = (html[:1200] + "...") if len(html) > 1200 else html
            else:
                # 状態が取れた場合は確認用に冒頭だけ
                debug_snippet = body_text[:300]

            await context.close()
            await browser.close()
            logging.info("playwright status=%s", status)
            return status, debug_snippet

    except Exception as e:
        logging.exception("fetch_status error: %s", e)
        return None, f"error: {e!r}"


# ====== Telegram Handlers ======
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "王子店『ダーツ』空席ウォッチです。\n"
        "/on で通知ON、/off で通知OFF、/status で現在の状況。\n"
        "/debug は解析用です。"
    )
    await update.message.reply_text(text)


async def cmd_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    _subs.add(chat_id)
    save_state()
    await update.message.reply_text("通知を ON にしました。")


async def cmd_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    _subs.discard(chat_id)
    save_state()
    await update.message.reply_text("通知を OFF にしました。")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with _fetch_lock:
        status, _ = await fetch_status_via_playwright()
    ts = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    if status:
        await update.message.reply_text(f"現在のダーツ: {status}（{ts}）")
    else:
        await update.message.reply_text("取得に失敗しました。後でもう一度。")


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with _fetch_lock:
        status, snippet = await fetch_status_via_playwright()
    lines = [
        f"status={status}",
        f"URL={SHOP_URL}",
        "--- debug ---",
        snippet,
    ]
    await update.message.reply_text("\n".join(lines))


# ====== ジョブ（定期ポーリング） ======
async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    global _last_status
    async with _fetch_lock:
        status, _ = await fetch_status_via_playwright()

    logging.info("poll: status %s", status)
    if status is None:
        return

    if status != _last_status:
        _last_status = status
        save_state()
        ts = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        msg = f"【更新】王子店ダーツ: {status}（{ts}）\n{SHOP_URL}"
        for chat_id in list(_subs):
            try:
                await context.bot.send_message(chat_id=chat_id, text=msg)
            except Exception as e:
                logging.warning("notify failed chat=%s err=%s", chat_id, e)


# ====== 起動 ======
def main():
    load_state()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("on", cmd_on))
    app.add_handler(CommandHandler("off", cmd_off))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("debug", cmd_debug))

    # 2分ごとに差分監視
    app.job_queue.run_repeating(poll_job, interval=CHECK_INTERVAL_SEC, first=5)

    logging.info("Bot starting…")
    # 他インスタンスとの衝突を避けるためWebhookは使わず、ロングポーリングのみ
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
