# -*- coding: utf-8 -*-
"""
快活クラブ 王子店『ダーツ』空席ウォッチ（Telegram × Playwright × Koyeb）
- /start : 使い方
- /on    : 通知ON
- /off   : 通知OFF
- /status: いまの状況を1回取得
- /debug : 解析用スニペット

環境変数:
  BOT_TOKEN        : Telegram Botのトークン（必須）
  TARGET_URL       : 監視URL（既定: 王子店の空席ページ）
  TARGET_LABEL     : ラベル（"ダーツ"）
  CHECK_INTERVAL_SEC: チェック間隔(秒) 既定120
"""
from __future__ import annotations
import os, json, re, asyncio, logging, traceback
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

from telegram import Update
from telegram.ext import ApplicationBuilder, Application, CommandHandler, ContextTypes
from playwright.async_api import async_playwright

# ===== 環境変数 =====
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "").strip()
TARGET_URL = os.environ.get("TARGET_URL", "https://www.kaikatsu.jp/shop/detail/vacancy.html?store_code=20328").strip()
TARGET_LABEL = os.environ.get("TARGET_LABEL", "ダーツ").strip()
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "120").strip() or "120")

DATA_DIR  = os.environ.get("DATA_DIR", "./data")
SUBS_FILE = os.path.join(DATA_DIR, "subs.json")
os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kaikatsu-bot")

# ===== 保存/読込 =====
def jst_now() -> str:
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst).strftime("%Y-%m-%d %H:%M:%S")

def load_subs() -> set[int]:
    try:
        with open(SUBS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_subs(s: set[int]) -> None:
    try:
        with open(SUBS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(s), f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("save_subs failed: %s", e)

SUBSCRIBERS: set[int] = load_subs()
LAST_STATUS: Optional[str] = None

_Z2H = str.maketrans("０１２３４５６７８９", "0123456789")
def norm_spaces(s: str) -> str:
    return re.sub(r"[\u3000\t ]+", " ", s.translate(_Z2H))

# ===== 取得 & 解析（Playwright: JS後の本文を読む） =====
async def fetch_status(debug: bool=False) -> Tuple[Optional[str], Optional[str]]:
    snippet = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = await browser.new_context(
                locale="ja-JP",
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0 Safari/537.36"),
                java_script_enabled=True,
                viewport={"width": 1280, "height": 960},
            )
            page = await ctx.new_page()
            await page.goto(TARGET_URL, wait_until="networkidle", timeout=45000)

            # Cookie等のボタンがあれば閉じる（あれば）
            for sel in ["#onetrust-accept-btn-handler", ".btn-accept", "button:has-text('OK')", "button:has-text('同意')"]:
                try:
                    await page.locator(sel).click(timeout=1000)
                    break
                except Exception:
                    pass

            await page.wait_for_timeout(800)
            text = await page.evaluate("document.body.innerText")
            await ctx.close(); await browser.close()

        t = norm_spaces(text)
        pat = re.compile(r"(満席|残\s*\d+\s*席(?:以上)?)")
        lines = t.splitlines()
        for i, ln in enumerate(lines):
            if TARGET_LABEL in ln:
                m = pat.search(ln)
                if m:
                    return m.group(1), (norm_spaces(ln)[:200] if debug else None)
                ctx2 = " ".join(lines[i:i+3])
                m2 = pat.search(ctx2)
                if m2:
                    return m2.group(1), (norm_spaces(ctx2)[:200] if debug else None)

        m3 = re.search(rf"{re.escape(TARGET_LABEL)}.*?(満席|残\s*\d+\s*席(?:以上)?)", t, re.S)
        if m3:
            return m3.group(1), (norm_spaces(t)[:300] if debug else None)

        return None, (norm_spaces(t)[:600] if debug else None)

    except Exception as e:
        return None, f"error: {e}\n{traceback.format_exc(limit=2)}"

# ===== Telegram コマンド =====
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        f"『{TARGET_LABEL}』空席ウォッチ\n"
        "/on で通知ON、/off で通知OFF、/status 現在の状況、/debug 解析用"
    )

async def cmd_on(u: Update, c: ContextTypes.DEFAULT_TYPE):
    SUBSCRIBERS.add(u.effective_chat.id); save_subs(SUBSCRIBERS)
    await u.message.reply_text("通知を ON にしました。")

async def cmd_off(u: Update, c: ContextTypes.DEFAULT_TYPE):
    SUBSCRIBERS.discard(u.effective_chat.id); save_subs(SUBSCRIBERS)
    await u.message.reply_text("通知を OFF にしました。")

async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE):
    status, _ = await fetch_status(False)
    await u.message.reply_text(
        f"現在: {status}（{jst_now()}）\n{TARGET_URL}" if status else "取得に失敗しました。"
    )

async def cmd_debug(u: Update, c: ContextTypes.DEFAULT_TYPE):
    status, snip = await fetch_status(True)
    msg = f"status={status}\nURL={TARGET_URL}"
    if snip:
        msg += f"\n--- debug ---\n{snip}"
    await u.message.reply_text(msg)

# ===== ポーリング =====
async def poll_job(ctx: ContextTypes.DEFAULT_TYPE):
    global LAST_STATUS
    status, _ = await fetch_status(False)
    if not status:
        return
    if status != LAST_STATUS:
        LAST_STATUS = status
        text = f"【更新】{TARGET_LABEL}: {status}（{jst_now()}）\n{TARGET_URL}"
        for cid in list(SUBSCRIBERS):
            try:
                await ctx.bot.send_message(cid, text)
            except Exception as e:
                log.warning("send failed %s: %s", cid, e)

def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("on",     cmd_on))
    app.add_handler(CommandHandler("off",    cmd_off))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("debug",  cmd_debug))
    app.job_queue.run_repeating(poll_job, interval=CHECK_INTERVAL_SEC, first=5)
    return app

def main():
    if not BOT_TOKEN:
        raise RuntimeError("環境変数 BOT_TOKEN が未設定です。")
    app = build_app()
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
