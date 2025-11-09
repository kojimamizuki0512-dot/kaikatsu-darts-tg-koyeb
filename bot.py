# -*- coding: utf-8 -*-
"""
快活クラブ 王子店『ダーツ』空席ウォッチ（Telegram版）
ボタン：通知ON/OFF 切替、今すぐ取得（同一メッセージを編集）

依存：
  python-telegram-bot[job-queue]==20.7
  playwright==1.47.0
  (Docker では chromium を --with-deps で導入済)

環境変数：
  TELEGRAM_BOT_TOKEN
  SHOP_URL
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from playwright.async_api import async_playwright

# ========= 設定 =========
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
URL = os.getenv("SHOP_URL", "https://www.kaikatsu.jp/shop/detail/vacancy.html?store_code=20328")
CHECK_INTERVAL_SEC = 120
SUBS_FILE = "subs.json"
TZ = ZoneInfo("Asia/Tokyo")

# ========= ロギング =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kaikatsu-bot")

# ========= 状態 =========
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

LAST_STATUS_STR: Optional[str] = None   # 例: "満席" / "残 2 席"
LAST_AT: Optional[datetime] = None      # JST

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
    # ボタンは「現在の状態に応じた“次の操作”」を出す
    toggle_label = "⛔ 通知OFF" if is_on else "✅ 通知ON"
    kb = [
        [InlineKeyboardButton(toggle_label, callback_data="toggle")],
        [InlineKeyboardButton("🔄 今すぐ取得", callback_data="refresh")],
    ]
    return InlineKeyboardMarkup(kb)

def menu_text(is_on: bool) -> str:
    return (
        "快活クラブ『ダーツ』空席ウォッチ。\n"
        "下のボタンで通知ON/OFFの切替や、今すぐ取得ができます。\n"
        f"現在: {onoff_emoji(is_on)} 通知{'ON' if is_on else 'OFF'}\n\n"
        f"{status_line()}"
    )

# ========= 取得＆解析 =========
async def _scrape_once() -> Tuple[Optional[str], Optional[datetime], Optional[str]]:
    """
    成功: (status, now_jst, snippet)
    失敗: (None, None, snippet or err)
    """
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

            # Cookieバナー等があれば閉じる（無ければ無視）
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
    # タイムアウト付きで一回実行
    try:
        st, at, _ = await asyncio.wait_for(_scrape_once(), timeout=60)
        return st, at
    except asyncio.TimeoutError:
        return None, None

# ========= メニュー送出/編集 =========
async def send_menu(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    is_on = chat_id in SUBSCRIBERS
    await ctx.bot.send_message(chat_id, menu_text(is_on), reply_markup=build_keyboard(is_on))

async def edit_menu(message, is_on: bool) -> None:
    try:
        await message.edit_text(menu_text(is_on), reply_markup=build_keyboard(is_on))
    except Exception as e:
        log.warning("edit_menu failed: %s", e)

# ========= ジョブ（状態変化時のみ通知） =========
async def poll_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global LAST_STATUS_STR, LAST_AT
    st, at = await fetch_status()
    log.info("poll: fetched=%s", st)
    if not st:
        return
    if st != LAST_STATUS_STR:
        LAST_STATUS_STR, LAST_AT = st, at
        text = f"【更新】王子店ダーツ: {st}（{fmt_jst(at)}）\n{URL}"
        # 購読者へ一斉送信（失敗は握りつぶす）
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
    # 「開始」「スタート」で /start 相当
    await send_menu(update.effective_chat.id, ctx)

async def on_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """通知ON/OFFボタン。ON→取得して反映、OFF→取得せずキャッシュだけ表示して即OFF。"""
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    is_on = chat_id in SUBSCRIBERS

    if is_on:
        # -> OFF（新規取得しない）
        SUBSCRIBERS.discard(chat_id)
        _save_subs(SUBSCRIBERS)
        await edit_menu(q.message, False)
        await ctx.bot.send_message(chat_id, "通知を OFF にしました。")
    else:
        # -> ON（最新を取得してから反映）
        st, at = await fetch_status()
        if st:
            global LAST_STATUS_STR, LAST_AT
            LAST_STATUS_STR, LAST_AT = st, at
        SUBSCRIBERS.add(chat_id)
        _save_subs(SUBSCRIBERS)
        await edit_menu(q.message, True)
        await ctx.bot.send_message(chat_id, "通知を ON にしました。")

async def on_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """今すぐ取得：同一メッセージを編集して結果を反映（新規メッセージは出さない）"""
    q = update.callback_query
    await q.answer("更新中…")
    # 一時的に“取得中…”に置き換え
    try:
        await q.message.edit_text("取得中…（最大 ~60 秒）")
    except Exception:
        pass

    st, at = await fetch_status()
    if st:
        global LAST_STATUS_STR, LAST_AT
        LAST_STATUS_STR, LAST_AT = st, at

    is_on = q.message.chat_id in SUBSCRIBERS
    await edit_menu(q.message, is_on)

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # /status は即時取得して返信（従来通り）
    st, at = await fetch_status()
    if st:
        global LAST_STATUS_STR, LAST_AT
        LAST_STATUS_STR, LAST_AT = st, at
        await update.message.reply_text(status_line())
    else:
        await update.message.reply_text("取得に失敗しました。")

# ========= 構築＆起動 =========
def build_app() -> Application:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("status", cmd_status))
    # 日本語トリガー
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
