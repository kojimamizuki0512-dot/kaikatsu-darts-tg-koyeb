import os, json, asyncio, re, datetime as dt
from pathlib import Path
from typing import Optional
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from playwright.async_api import async_playwright

SHOP_URL = os.getenv("SHOP_URL", "https://www.kaikatsu.jp/shop/detail/vacancy.html?store_code=20328")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

STATE_PATH = Path("state.json")
SUBS_PATH = Path("subs.json")
CHECK_INTERVAL_SEC = 120  # 2分ごと

def load_json(p: Path, default):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default

def save_json(p: Path, data):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

state = load_json(STATE_PATH, {"last_status": None, "last_changed_at": None})
subs = set(load_json(SUBS_PATH, []))

async def fetch_status() -> Optional[str]:
    """
    快活の空席ページを Playwright(Chromium) で開き、
    ページ内テキストから「ダーツ」行の数字を抽出して返す。
    例: "残1席" / "満席" / None(取得失敗)
    """
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(SHOP_URL, wait_until="domcontentloaded", timeout=30000)

            # vacancy_area.js を辿る方式は店舗によって分岐するため、まずは画面全体テキストから拾う
            text = await page.inner_text("body")
            await browser.close()

        # 一番素直に「ダーツ」「残」「満」あたりで拾う
        # 例: 「ダーツ 残1席」, 「ダーツ 満席」
        m = re.search(r"ダーツ[^\\n]*?(残\s*\d+\s*席|満席)", text)
        if not m:
            return None
        word = m.group(1)
        word = re.sub(r"\s+", "", word)  # 空白除去 「残1席」「満席」
        return word
    except Exception:
        return None

async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "王子店『ダーツ』空席ウォッチです。\n"
        "/on で通知ON、/off で通知OFF、/status で現在の状況。\n"
        "/debug は解析用です。"
    )

async def cmd_on(update: Update, _: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subs.add(chat_id)
    save_json(SUBS_PATH, list(subs))
    await update.message.reply_text("通知を ON にしました。")

async def cmd_off(update: Update, _: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subs.discard(chat_id)
    save_json(SUBS_PATH, list(subs))
    await update.message.reply_text("通知を OFF にしました。")

async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE):
    s = await fetch_status()
    if s:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await update.message.reply_text(f"現在のダーツ: {s}（{now}）")
    else:
        await update.message.reply_text("取得に失敗しました。後でもう一度。")

async def cmd_debug(update: Update, _: ContextTypes.DEFAULT_TYPE):
    s = await fetch_status()
    await update.message.reply_text(
        f"status={s}\nURL={SHOP_URL}"
    )

async def poll_job(_: ContextTypes.DEFAULT_TYPE):
    s = await fetch_status()
    if s is None:
        return
    last = state.get("last_status")
    if last != s:
        state["last_status"] = s
        state["last_changed_at"] = dt.datetime.now().isoformat(timespec="seconds")
        save_json(STATE_PATH, state)
        # 変化したら全購読者へ通知
        text = f"【ダーツ 空席状況が変化】\n現在: {s}"
        for cid in list(subs):
            try:
                await app.bot.send_message(chat_id=cid, text=text)
            except Exception:
                pass

def build_app():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("on", cmd_on))
    application.add_handler(CommandHandler("off", cmd_off))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("debug", cmd_debug))
    # JobQueue
    application.job_queue.run_repeating(poll_job, interval=CHECK_INTERVAL_SEC, first=5)
    return application

app = build_app()

if __name__ == "__main__":
    print("Bot starting…")
    app.run_polling(drop_pending_updates=True)
