# -*- coding: utf-8 -*-
"""
快活クラブ 王子店『ダーツ』空席ウォッチ（Telegram版）
/start /menu /on /off /status /debug /ping
インラインボタン:
  - ✅/⛔ 通知ON/OFF をワンタップ切替（表示も即更新）
  - 🔄 今すぐ取得（/status 相当）→ 同メッセージを編集
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import traceback
from datetime import datetime
from typing import Optional, Tuple

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler,
    CallbackQueryHandler, ContextTypes
)
from playwright.async_api import async_playwright

# ========= 環境変数 =========
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
URL = os.getenv("SHOP_URL", "https://www.kaikatsu.jp/shop/detail/vacancy.html?store_code=20328")
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "120"))
SUBS_FILE = os.getenv("SUBS_FILE", "subs.json")

# ========= ロギング =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("kaikatsu-bot")

# ========= 通知先の保存 =========
def load_subs() -> set[int]:
    try:
        with open(SUBS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(int(x) for x in data)
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

# 直列ロック（手動/status と定期ジョブのバッティング回避）
SCRAPE_LOCK = asyncio.Lock()

# ========= ユーティリティ =========
_Z2H = str.maketrans("０１２３４５６７８９", "0123456789")

def norm_spaces(s: str) -> str:
    s = s.translate(_Z2H)
    return re.sub(r"[\u3000\t ]+", " ", s)

def now_jp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def is_subscribed(chat_id: int) -> bool:
    return chat_id in SUBSCRIBERS

def menu_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    on = is_subscribed(chat_id)
    label_toggle = f"{'✅' if on else '⛔'} 通知: {'ON' if on else 'OFF'}（タップで切替）"
    btn_toggle = InlineKeyboardButton(label_toggle, callback_data="toggle_notify")
    btn_fetch  = InlineKeyboardButton("🔄 今すぐ取得", callback_data="fetch_now")
    # 1行に2ボタン
    return InlineKeyboardMarkup([[btn_toggle, btn_fetch]])

# ========= 取得＆解析 =========
async def fetch_status(debug: bool = False, timeout_sec: int = 60) -> Tuple[Optional[str], Optional[str]]:
    """
    成功: (status文字列, デバッグ用スニペット)
    失敗: (None, 例外メッセージ/スニペット)
    """
    async def _scrape_once() -> Tuple[Optional[str], Optional[str]]:
        snippet = None
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

            # Cookie などのバナーがあれば閉じる（なければ無視）
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

        if debug:
            return None, norm_spaces(t)[:700]
        return None, None

    try:
        async with SCRAPE_LOCK:
            return await asyncio.wait_for(_scrape_once(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        return None, "timeout"
    except Exception as e:
        err = f"error: {e}\n{traceback.format_exc(limit=2)}"
        return None, err

# ========= コマンド =========
INTRO = (
    "王子店『ダーツ』空席ウォッチです。\n"
    "/on で通知ON、/off で通知OFF、/status で現在の状況、/debug は解析用、/ping は疎通チェックです。\n"
    "下のボタンで通知ON/OFFの切替や、今すぐ取得ができます。"
)

async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = u.effective_chat.id
    await u.message.reply_text(INTRO, reply_markup=menu_keyboard(chat_id))

async def cmd_menu(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = u.effective_chat.id
    await u.message.reply_text("メニュー：", reply_markup=menu_keyboard(chat_id))

async def cmd_ping(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await u.message.reply_text(f"pong ({now_jp()})")

async def cmd_on(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    SUBSCRIBERS.add(u.effective_chat.id)
    save_subs(SUBSCRIBERS)
    await u.message.reply_text("通知を ON にしました。")

async def cmd_off(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    SUBSCRIBERS.discard(u.effective_chat.id)
    save_subs(SUBSCRIBERS)
    await u.message.reply_text("通知を OFF にしました。")

async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await u.message.reply_text("取得中…（最大 ~60 秒）")
    status, _ = await fetch_status(False, timeout_sec=60)
    await u.message.reply_text(
        f"現在のダーツ: {status}（{now_jp()}）" if status else "取得に失敗しました。"
    )

async def cmd_debug(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await u.message.reply_text("取得中…（最大 ~60 秒）")
    status, snippet = await fetch_status(True, timeout_sec=60)
    msg = f"status={status}\nURL={URL}"
    if snippet:
        msg += f"\n--- debug ---\n{snippet}"
    await u.message.reply_text(msg)

# ========= インラインボタン =========
async def on_toggle_button(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    q = u.callback_query
    await q.answer()
    chat_id = q.message.chat_id

    if is_subscribed(chat_id):
        SUBSCRIBERS.discard(chat_id)
        save_subs(SUBSCRIBERS)
        note = "通知を OFF にしました。"
    else:
        SUBSCRIBERS.add(chat_id)
        save_subs(SUBSCRIBERS)
        note = "通知を ON にしました。"

    # ボタンだけ更新
    try:
        await q.edit_message_reply_markup(reply_markup=menu_keyboard(chat_id))
    except Exception:
        pass
    await q.message.reply_text(note)

async def on_fetch_now(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    q = u.callback_query
    await q.answer()
    chat_id = q.message.chat_id

    # まずメッセージを「取得中…」に編集（同じメッセージを使い回す）
    try:
        await q.edit_message_text("取得中…（最大 ~60 秒）", reply_markup=menu_keyboard(chat_id))
    except Exception:
        # 権限や古いメッセージで編集できない場合、別メッセージで案内
        await q.message.reply_text("取得中…（最大 ~60 秒）")

    status, _ = await fetch_status(False, timeout_sec=60)
    text = f"現在のダーツ: {status}（{now_jp()}）" if status else "取得に失敗しました。"

    # 結果で同じメッセージを上書き
    try:
        await q.edit_message_text(text, reply_markup=menu_keyboard(chat_id))
    except Exception:
        await q.message.reply_text(text, reply_markup=menu_keyboard(chat_id))

# ========= 監視ジョブ =========
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

# ========= 構築＆起動 =========
def build_app() -> Application:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN が未設定です。KoyebのEnvironment variablesを確認してください。")

    app = ApplicationBuilder().token(TOKEN).build()

    # コマンド
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("on", cmd_on))
    app.add_handler(CommandHandler("off", cmd_off))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("debug", cmd_debug))

    # コールバック
    app.add_handler(CallbackQueryHandler(on_toggle_button, pattern="^toggle_notify$"))
    app.add_handler(CallbackQueryHandler(on_fetch_now,   pattern="^fetch_now$"))

    # 定期ジョブ
    app.job_queue.run_repeating(poll_job, interval=CHECK_INTERVAL_SEC, first=10)
    return app

def main() -> None:
    app = build_app()
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
