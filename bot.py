# -*- coding: utf-8 -*-
"""
快活クラブ 王子店『ダーツ』空席ウォッチ（Telegram版）
- ボタン：通知ON/OFF 切替、今すぐ取得（同一メッセージ編集）
- 取得中はスピナー表示
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters
)
from playwright.async_api import async_playwright

# ========= 設定 =========
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
URL = os.getenv("SHOP_URL", "https://www.kaikatsu.jp/shop/detail/vacancy.html?store_code=20328")
CHECK_INTERVAL_SEC = 120
SUBS_FILE = "subs.json"

# --- JST（tzdata が無い環境でも動くフォールバック） ---
try:
    TZ = ZoneInfo("Asia/Tokyo")
except Exception:
    TZ = timezone(timedelta(hours=9))  # UTC+9

# ========= ロギング =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kaikatsu-bot")

# ========= 状態保存 =========
def _load_subs() -> set[int]:
    try:
        with open(SUBS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def _save_subs(s: set[int]) -> None:
    try:
        with open(SUBS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(s), f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("save_subs: %s", e)

SUBSCRIBERS: set[int] = _load_subs()
LAST_STATUS_STR: Optional[str] = None
LAST_AT: Optional[datetime] = None

# ========= ユーティリティ =========
_Z2H = str.maketrans("０１２３４５６７８９", "0123456789")

def norm_spaces(s: str) -> str:
    s = s.translate(_Z2H)
    return re.sub(r"[\u3000\t ]+", " ", s)

def fmt_jst(dt: Optional[datetime]) -> str:
    if not dt:
        return "未取得"
    return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")

def status_line() -> str:
    if LAST_STATUS_STR and LAST_AT:
        return f"現在のダーツ: {LAST_STATUS_STR}（{fmt_jst(LAST_AT)}）"
    return "現在のダーツ: 未取得"

def onoff_emoji(is_on: bool) -> str:
    return "🟢" if is_on else "🔴"

def build_keyboard(is_on: bool) -> InlineKeyboardMarkup:
    # 次の操作を出す
    toggle_label = "⛔ 通知OFF" if is_on else "✅ 通知ON"
    kb = [
        [InlineKeyboardButton(toggle_label, callback_data="toggle")],
        [InlineKeyboardButton("🔄 今すぐ取得", callback_data="refresh")],
    ]
    return InlineKeyboardMarkup(kb)

def menu_text(is_on: bool) -> str:
    return (
        "快活クラブ『ダーツ』空席ウォッチ。下のボタンで通知ON/OFFの切替や、今すぐ取得ができます。\n"
        f"現在: {onoff_emoji(is_on)} 通知{'ON' if is_on else 'OFF'}\n\n"
        f"{status_line()}"
    )

# ========= スクレイプ =========
async def _scrape_once() -> Tuple[Optional[str], Optional[datetime], Optional[str]]:
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
            )
            page = await ctx.new_page()
            await page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            for sel in ["#onetrust-accept-btn-handler", ".btn-accept", "button.accept"]:
                try:
                    await page.locator(sel).click(timeout=800)
                    break
                except Exception:
                    pass
            await page.wait_for_timeout(1200)
            body_text = await page.evaluate("document.body.innerText")
            await browser.close()

        t = norm_spaces(body_text)
        pat = re.compile(r"(満席|残\s*\d+\s*席(?:以上)?)")

        lines = t.splitlines()
        for i, ln in enumerate(lines):
            if "ダーツ" in ln:
                m = pat.search(ln)
                if m:
                    return m.group(1), datetime.now(TZ), None
                ctx = " ".join(lines[i:i+3])
                m = pat.search(ctx)
                if m:
                    return m.group(1), datetime.now(TZ), None

        m = re.search(r"ダーツ.*?(満席|残\s*\d+\s*席(?:以上)?)", t, re.S)
        if m:
            return m.group(1), datetime.now(TZ), None

        snippet = norm_spaces(t)[:600]
        return None, None, snippet

    except Exception as e:
        err = f"error: {e}\n{traceback.format_exc(limit=2)}"
        return None, None, err

async def fetch_status() -> Tuple[Optional[str], Optional[datetime]]:
    try:
        st, at, _ = await asyncio.wait_for(_scrape_once(), timeout=60)
        return st, at
    except asyncio.TimeoutError:
        return None, None

# ========= メニュー編集 =========
async def edit_menu(message, is_on: bool) -> None:
    try:
        await message.edit_text(menu_text(is_on), reply_markup=build_keyboard(is_on))
    except Exception as e:
        log.warning("edit_menu failed: %s", e)

async def send_menu(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    is_on = chat_id in SUBSCRIBERS
    await ctx.bot.send_message(chat_id, menu_text(is_on), reply_markup=build_keyboard(is_on))

# ========= スピナー =========
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

async def animate_spinner(message, stop_event: asyncio.Event):
    i = 0
    while not stop_event.is_set():
        try:
            await message.edit_text(f"取得中… {SPINNER_FRAMES[i % len(SPINNER_FRAMES)]}")
        except Exception:
            pass
        await asyncio.sleep(0.6)
        i += 1

# ========= 定期ジョブ =========
async def poll_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global LAST_STATUS_STR, LAST_AT
    st, at = await fetch_status()
    log.info("poll: fetched=%s", st)
    if not st:
        return
    if st != LAST_STATUS_STR:
        LAST_STATUS_STR, LAST_AT = st, at
        text = f"【更新】王子店ダーツ: {st}（{fmt_jst(at)}）\n{URL}"
        for chat_id in list(SUBSCRIBERS):
            try:
                await ctx.bot.send_message(chat_id, text)
            except Exception as e:
                log.warning("send failed %s: %s", chat_id, e)

# ========= ハンドラ =========
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await send_menu(update.effective_chat.id, ctx)

async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"pong（{fmt_jst(datetime.now(TZ))}）")

async def on_text_start_like(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await send_menu(update.effective_chat.id, ctx)

async def on_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """ON→取得して反映（同じメッセ編集）。OFF→取得せずキャッシュで即OFF（同じメッセ編集）。"""
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    is_on = chat_id in SUBSCRIBERS

    if is_on:
        # -> OFF（新規取得しない／新規メッセージは送らない）
        SUBSCRIBERS.discard(chat_id)
        _save_subs(SUBSCRIBERS)
        await edit_menu(q.message, False)
    else:
        # -> ON（スピナー→取得→同じメッセ編集）
        stop = asyncio.Event()
        spinner_task = asyncio.create_task(animate_spinner(q.message, stop))
        st, at = await fetch_status()
        stop.set()
        try:
            await asyncio.wait_for(spinner_task, timeout=1)
        except Exception:
            pass

        if st:
            global LAST_STATUS_STR, LAST_AT
            LAST_STATUS_STR, LAST_AT = st, at
        SUBSCRIBERS.add(chat_id)
        _save_subs(SUBSCRIBERS)
        await edit_menu(q.message, True)

async def on_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """今すぐ取得：同一メッセージをスピナー表示→完了後メニューに再編集"""
    q = update.callback_query
    await q.answer("更新中…")
    stop = asyncio.Event()
    spinner_task = asyncio.create_task(animate_spinner(q.message, stop))

    st, at = await fetch_status()
    stop.set()
    try:
        await asyncio.wait_for(spinner_task, timeout=1)
    except Exception:
        pass

    if st:
        global LAST_STATUS_STR, LAST_AT
        LAST_STATUS_STR, LAST_AT = st, at

    is_on = q.message.chat_id in SUBSCRIBERS
    await edit_menu(q.message, is_on)

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    st, at = await fetch_status()
    if st:
        global LAST_STATUS_STR, LAST_AT
        LAST_STATUS_STR, LAST_AT = st, at
        await update.message.reply_text(status_line())
    else:
        await update.message.reply_text("取得に失敗しました。")

# ========= 起動 =========
def build_app() -> Application:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(開始|スタート)$"), on_text_start_like))
    app.add_handler(CallbackQueryHandler(on_toggle, pattern="^toggle$"))
    app.add_handler(CallbackQueryHandler(on_refresh, pattern="^refresh$"))

    app.job_queue.run_repeating(poll_job, interval=CHECK_INTERVAL_SEC, first=5, name="poll_job")
    return app

def main() -> None:
    app = build_app()
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
