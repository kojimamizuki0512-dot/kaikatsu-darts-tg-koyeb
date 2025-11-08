# -*- coding: utf-8 -*-
"""
快活クラブ 王子店『ダーツ』空席ウォッチ（Telegram版）
ボタン2行：①通知トグル（同じメッセージを編集） ②今すぐ取得（新規メッセージ）
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import traceback
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters,
)
from playwright.async_api import async_playwright

# ====== 環境変数 ======
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
URL = os.getenv("SHOP_URL", "https://www.kaikatsu.jp/shop/detail/vacancy.html?store_code=20328")
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "120"))
SUBS_FILE = os.getenv("SUBS_FILE", "subs.json")

# ====== ログ ======
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("kaikatsu-bot")

# ====== 購読管理 ======
def load_subs() -> set[int]:
    try:
        with open(SUBS_FILE, "r", encoding="utf-8") as f:
            return set(int(x) for x in json.load(f))
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
SCRAPE_LOCK = asyncio.Lock()

# ====== 共通ユーティリティ ======
_Z2H = str.maketrans("０１２３４５６７８９", "0123456789")
JST = timezone(timedelta(hours=9), name="JST")

def norm_spaces(s: str) -> str:
    s = s.translate(_Z2H)
    return re.sub(r"[\u3000\t ]+", " ", s)

def now_jp() -> str:
    return datetime.now(tz=JST).strftime("%Y-%m-%d %H:%M:%S")

def is_subscribed(chat_id: int) -> bool:
    return chat_id in SUBSCRIBERS

INTRO = "快活クラブ『ダーツ』空席ウォッチ。下のボタンで通知ON/OFFの切替や、今すぐ取得ができます。"

def status_line(chat_id: int) -> str:
    return "現在: 🟢 通知ON" if is_subscribed(chat_id) else "現在: 🔴 通知OFF"

def format_menu_text(chat_id: int, extra: str | None = None) -> str:
    text = f"{INTRO}\n{status_line(chat_id)}"
    if extra:
        text += f"\n\n{extra}"
    return text

def menu_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    on = is_subscribed(chat_id)
    # ボタンは「次に起こる動作」を表示（ONの時はOFFボタンを見せる）
    label_toggle = "⛔ 通知OFF" if on else "✅ 通知ON"
    btn_toggle = InlineKeyboardButton(label_toggle, callback_data="toggle_notify")
    btn_fetch  = InlineKeyboardButton("🔄 今すぐ取得", callback_data="fetch_now")
    return InlineKeyboardMarkup([[btn_toggle], [btn_fetch]])

# ====== 取得ロジック ======
async def fetch_status(debug: bool = False, timeout_sec: int = 60) -> Tuple[Optional[str], Optional[str]]:
    async def _scrape_once() -> Tuple[Optional[str], Optional[str]]:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = await browser.new_context(
                locale="ja-JP",
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
                java_script_enabled=True,
            )
            page = await ctx.new_page()
            await page.goto(URL, wait_until="domcontentloaded", timeout=45000)

            for sel in ["#onetrust-accept-btn-handler", ".btn-accept", "button.accept"]:
                try:
                    await page.locator(sel).click(timeout=1000)
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
                    return m.group(1), (norm_spaces(ln)[:200] if debug else None)
                ctx = " ".join(lines[i:i+3])
                m = pat.search(ctx)
                if m:
                    return m.group(1), (norm_spaces(ctx)[:200] if debug else None)

        m = re.search(r"ダーツ.*?(満席|残\s*\d+\s*席(?:以上)?)", t, re.S)
        if m:
            return m.group(1), (norm_spaces(t)[:300] if debug else None)

        return (None, norm_spaces(t)[:700] if debug else None)

    try:
        async with SCRAPE_LOCK:
            return await asyncio.wait_for(_scrape_once(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        return None, "timeout"
    except Exception as e:
        return None, f"error: {e}\n{traceback.format_exc(limit=2)}"

# ====== メニュー送信ユーティリティ ======
async def send_menu_message(chat_id: int, c: ContextTypes.DEFAULT_TYPE, extra: str | None = None):
    await c.bot.send_message(chat_id, format_menu_text(chat_id, extra), reply_markup=menu_keyboard(chat_id))

# ====== コマンド ======
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await send_menu_message(u.effective_chat.id, c)

async def cmd_menu(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await send_menu_message(u.effective_chat.id, c)

async def cmd_on(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    SUBSCRIBERS.add(u.effective_chat.id)
    save_subs(SUBSCRIBERS)
    status, _ = await fetch_status(False, timeout_sec=60)
    extra = f"現在のダーツ: {status}（{now_jp()}）" if status else "取得に失敗しました。"
    await send_menu_message(u.effective_chat.id, c, extra=extra)

async def cmd_off(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    SUBSCRIBERS.discard(u.effective_chat.id)
    save_subs(SUBSCRIBERS)
    await send_menu_message(u.effective_chat.id, c)

async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    status, _ = await fetch_status(False, timeout_sec=60)
    extra = f"現在のダーツ: {status}（{now_jp()}）" if status else "取得に失敗しました。"
    await send_menu_message(u.effective_chat.id, c, extra=extra)

async def cmd_debug(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    status, snippet = await fetch_status(True, timeout_sec=60)
    msg = f"status={status}\nURL={URL}"
    if snippet:
        msg += f"\n--- debug ---\n{snippet}"
    await u.message.reply_text(msg)

# 日本語キーワードでメニュー
_JP_MENU_WORDS = ("スタート", "開始", "メニュー", "めにゅー", "menu", "start", "help")
async def on_text_keywords(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not u.message or not (txt := (u.message.text or "").strip()):
        return
    if u.effective_chat.type != "private":
        return
    if any(w.lower() in txt.lower() for w in _JP_MENU_WORDS):
        await send_menu_message(u.effective_chat.id, c)

# ====== インラインボタン ======
async def on_toggle_button(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    q = u.callback_query
    await q.answer()
    chat_id = q.message.chat_id

    # 現在状態を確認してトグル
    if is_subscribed(chat_id):
        # → OFF：取得はしない／同じメッセージを編集
        SUBSCRIBERS.discard(chat_id)
        save_subs(SUBSCRIBERS)
        try:
            await q.edit_message_text(
                text=format_menu_text(chat_id),
                reply_markup=menu_keyboard(chat_id),
            )
        except Exception as e:
            log.warning("edit OFF failed: %s", e)
        return

    # → ON：まず即座に「取得中…」で同じメッセージを編集
    SUBSCRIBERS.add(chat_id)
    save_subs(SUBSCRIBERS)
    try:
        await q.edit_message_text(
            text=format_menu_text(chat_id, extra="取得中…（最大 ~60 秒）"),
            reply_markup=menu_keyboard(chat_id),
        )
    except Exception as e:
        log.warning("edit ON (loading) failed: %s", e)

    # 取得して結果で再編集
    status, _ = await fetch_status(False, timeout_sec=60)
    extra = f"現在のダーツ: {status}（{now_jp()}）" if status else "取得に失敗しました。"
    try:
        await q.edit_message_text(
            text=format_menu_text(chat_id, extra=extra),
            reply_markup=menu_keyboard(chat_id),
        )
    except Exception as e:
        log.warning("edit ON (result) failed: %s", e)

async def on_fetch_now(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    q = u.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    status, _ = await fetch_status(False, timeout_sec=60)
    extra = f"現在のダーツ: {status}（{now_jp()}）" if status else "取得に失敗しました。"
    # 「今すぐ取得」は新しいメッセージで返す（現行運用）
    await send_menu_message(chat_id, c, extra=extra)

# ====== 監視ジョブ ======
async def poll_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global LAST_STATUS
    status, _ = await fetch_status(False, timeout_sec=60)
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

# ====== 起動 ======
def build_app() -> Application:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN が未設定です。KoyebのEnvironment variablesを確認してください。")
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("menu",   cmd_menu))
    app.add_handler(CommandHandler("on",     cmd_on))
    app.add_handler(CommandHandler("off",    cmd_off))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("debug",  cmd_debug))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_keywords))
    app.add_handler(CallbackQueryHandler(on_toggle_button, pattern="^toggle_notify$"))
    app.add_handler(CallbackQueryHandler(on_fetch_now,     pattern="^fetch_now$"))

    app.job_queue.run_repeating(poll_job, interval=CHECK_INTERVAL_SEC, first=10)
    return app

def main() -> None:
    app = build_app()
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
