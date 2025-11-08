# -*- coding: utf-8 -*-
"""
快活クラブ 王子店『ダーツ』空席ウォッチ（Telegram版）
/start  /on  /off  /status  /debug

ベース: Playwright 1.47.0（公式Docker）, PTB 20.7
"""

from __future__ import annotations
import os
import json
import logging
import re
import traceback
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

from telegram import Update
from telegram.ext import ApplicationBuilder, Application, CommandHandler, ContextTypes
from playwright.async_api import async_playwright

# ========= 設定（必ずKoyebの環境変数で渡す）=========
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
URL = os.environ.get("SHOP_URL", "https://www.kaikatsu.jp/shop/detail/vacancy.html?store_code=20328")
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "120"))
SUBS_FILE = "subs.json"

JST = timezone(timedelta(hours=9))

# ========= ロギング =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kaikatsu-bot")

# ========= 通知先の保存 =========
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
        log.warning("save_subs: %s", e)

SUBSCRIBERS: set[int] = load_subs()
LAST_STATUS: Optional[str] = None

# ========= ユーティリティ =========
_Z2H = str.maketrans("０１２３４５６７８９", "0123456789")

def norm_spaces(s: str) -> str:
    s = s.translate(_Z2H)
    return re.sub(r"[\u3000\t ]+", " ", s)

def now_jp() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")

# ========= 取得＆解析（PlaywrightでJS実行後の本文を読む） =========
async def fetch_status(debug: bool = False) -> Tuple[Optional[str], Optional[str]]:
    """
    成功: (status文字列, デバッグ用スニペット)
    失敗: (None, 例外メッセージ/スニペット)
    """
    snippet = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
            ctx = await browser.new_context(
                locale="ja-JP",
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0 Safari/537.36"),
                java_script_enabled=True,
            )
            page = await ctx.new_page()
            await page.goto(URL, wait_until="domcontentloaded", timeout=45000)

            # Cookieバナーがあれば閉じる（無ければスルー）
            for sel in ["#onetrust-accept-btn-handler", ".btn-accept", "button.accept"]:
                try:
                    await page.locator(sel).click(timeout=1000)
                    break
                except Exception:
                    pass

            # レイアウトが落ち着くまで少し待つ
            await page.wait_for_timeout(1200)

            body_text = await page.evaluate("document.body.innerText")
            await browser.close()

        t = norm_spaces(body_text)

        # 近傍抽出：「ダーツ」行の近くから 満席 / 残X席(以上) を探す
        pat = re.compile(r"(満席|残\s*\d+\s*席(?:以上)?)")
        lines = t.splitlines()
        for i, ln in enumerate(lines):
            if "ダーツ" in ln:
                m = pat.search(ln)
                if m:
                    return m.group(1), (norm_spaces(ln)[:200] if debug else None)
                ctx = " ".join(lines[i:i+3])
                m = pat.search(ctx)
                if m:
                    return m.group(1), (norm_spaces(ctx)[:200] if debug else None)

        # 全体からの緩め抽出
        m = re.search(r"ダーツ.*?(満席|残\s*\d+\s*席(?:以上)?)", t, re.S)
        if m:
            return m.group(1), (norm_spaces(t)[:300] if debug else None)

        if debug:
            snippet = norm_spaces(t)[:700]
        return None, snippet

    except Exception as e:
        err = f"error: {e}\n{traceback.format_exc(limit=2)}"
        return None, err

# ========= Telegram コマンド =========
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await u.message.reply_text(
        "王子店『ダーツ』空席ウォッチです。\n"
        "/on で通知ON、/off で通知OFF、/status で現在の状況を取得、/debug は解析用です。"
    )

async def cmd_on(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    SUBSCRIBERS.add(u.effective_chat.id)
    save_subs(SUBSCRIBERS)
    await u.message.reply_text("通知を ON にしました。")

async def cmd_off(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    SUBSCRIBERS.discard(u.effective_chat.id)
    save_subs(SUBSCRIBERS)
    await u.message.reply_text("通知を OFF にしました。")

async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    status, _ = await fetch_status(False)
    await u.message.reply_text(
        f"現在のダーツ: {status}（{now_jp()}）" if status else "取得に失敗しました。"
    )

async def cmd_debug(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    status, snippet = await fetch_status(True)
    msg = f"status={status}\nURL={URL}"
    if snippet:
        msg += f"\n--- debug ---\n{snippet}"
    await u.message.reply_text(msg)

# ========= 監視ジョブ =========
async def poll_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global LAST_STATUS
    status, _ = await fetch_status(False)
    if not status:
        return
    if status != LAST_STATUS:
        LAST_STATUS = status
        text = f"【更新】王子店ダーツ: {status}（{now_jp()}）\n{URL}"
        for chat_id in list(SUBSCRIBERS):
            try:
                await ctx.bot.send_message(chat_id, text)
            except Exception as e:
                log.warning("send failed %s: %s", chat_id, e)

def build_app() -> Application:
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("on", cmd_on))
    app.add_handler(CommandHandler("off", cmd_off))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.job_queue.run_repeating(poll_job, interval=CHECK_INTERVAL_SEC, first=5)
    return app

def main() -> None:
    app = build_app()
    # 409回避：ローカル/別インスタンスは必ず止める
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
