# -*- coding: utf-8 -*-
"""
快活クラブ 王子店『ダーツ』空席ウォッチ（Telegram 版）
- ワンタップで通知ON/OFF切替
- 「今すぐ取得」はメッセージを編集してスピナー→結果に更新
- 通知ON時のみポーリング通知（変化があれば新規メッセージで通知）

必要パッケージ（requirements.txt 例）:
python-telegram-bot[job-queue]==20.7
playwright==1.47.0
tzdata==2024.1
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
from zoneinfo import ZoneInfo
from typing import Optional, Tuple, Dict, Set

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from playwright.async_api import async_playwright

# ====== 設定 ======
# ★ここはあなたの現行のやり方（直書き or 環境変数）に合わせてください
TOKEN = os.getenv("BOT_TOKEN", "REPLACE_ME")  # 直書きの場合はここにトークンを入れる
URL = "https://www.kaikatsu.jp/shop/detail/vacancy.html?store_code=20328"
CHECK_INTERVAL_SEC = 120
SUBS_FILE = "subs.json"

TZ = ZoneInfo("Asia/Tokyo")

# ====== ログ ======
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kaikatsu-bot")

# ====== 状態 ======
def _load_json(path: str, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback

def _save_json(path: str, obj) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("save_json: %s", e)

SUBSCRIBERS: Set[int] = set(_load_json(SUBS_FILE, []))

@dataclass
class Last:
    status: Optional[str] = None      # 例: "満席" / "残 2 席"
    at: Optional[str] = None          # 例: "2025-11-14 18:12:34"

LAST_BY_CHAT: Dict[int, Last] = {}    # chat_id -> Last

# ====== ユーティリティ ======
_Z2H = str.maketrans("０１２３４５６７８９", "0123456789")

def norm_spaces(s: str) -> str:
    s = s.translate(_Z2H)
    return re.sub(r"[\u3000\t ]+", " ", s)

def now_jp_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

def is_on(chat_id: int) -> bool:
    return chat_id in SUBSCRIBERS

def set_on(chat_id: int, on: bool) -> None:
    if on:
        SUBSCRIBERS.add(chat_id)
    else:
        SUBSCRIBERS.discard(chat_id)
    _save_json(SUBS_FILE, list(SUBSCRIBERS))

def get_last(chat_id: int) -> Last:
    return LAST_BY_CHAT.setdefault(chat_id, Last())

def set_last(chat_id: int, status: Optional[str], at: Optional[str]) -> None:
    LAST_BY_CHAT[chat_id] = Last(status=status, at=at)

def build_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    # ボタンは「次の行動」を書く（ONならOFFボタンを出す）
    if is_on(chat_id):
        toggle = InlineKeyboardButton("⛔ 通知OFF", callback_data="toggle_off")
    else:
        toggle = InlineKeyboardButton("✅ 通知ON", callback_data="toggle_on")
    getnow = InlineKeyboardButton("🔄 今すぐ取得", callback_data="get_now")
    return InlineKeyboardMarkup([[toggle], [getnow]])

def render_menu_text(chat_id: int, fetching: bool = False) -> str:
    on = is_on(chat_id)
    lamp = "🟢 通知ON" if on else "🔴 通知OFF"
    last = get_last(chat_id)

    head = (
        "快活クラブ『ダーツ』空席ウォッチ。下のボタンで通知ON/OFFの切替や、今すぐ取得ができます。\n"
        f"現在: {lamp}\n"
    )

    if fetching:
        body = "現在のダーツ: 取得中…（最大 ~60 秒）"
    else:
        if last.status and last.at:
            body = f"現在のダーツ: {last.status}（{last.at}）"
        else:
            body = "現在のダーツ: 取得できていません"

    return f"{head}\n{body}"

# ====== 取得（Playwright） ======
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
        # 表示が遅いケースに備え追加待機
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        try:
            await page.wait_for_selector("text=ダーツ", timeout=8000)
        except Exception:
            pass

        text = await page.evaluate("document.body.innerText")
        await browser.close()

    t = norm_spaces(text)
    pat = re.compile(r"(満席|残\s*\d+\s*席(?:以上)?|受付停止|準備中|休止|営業時間外)")
    lines = t.splitlines()
    for i, ln in enumerate(lines):
        if "ダーツ" in ln:
            m = pat.search(ln)
            if m:
                return m.group(1), None
            ctx = " ".join(lines[i:i+3])
            m = pat.search(ctx)
            if m:
                return m.group(1), None

    m = re.search(r"ダーツ.*?(満席|残\s*\d+\s*席(?:以上)?|受付停止|準備中|休止|営業時間外)", t, re.S)
    if m:
        return m.group(1), None

    snippet = t[:700]
    return None, snippet

async def fetch_status() -> Tuple[Optional[str], Optional[str]]:
    """成功: (status, None) / 失敗: (None, snippet_or_error)"""
    try:
        return await asyncio.wait_for(_scrape_once(), timeout=45)
    except Exception as e:
        return None, f"error: {e}\n{traceback.format_exc(limit=1)}"

# ====== ハンドラ ======
async def send_or_edit_menu(u: Update, c: ContextTypes.DEFAULT_TYPE, fetching: bool = False) -> None:
    chat_id = u.effective_chat.id
    text = render_menu_text(chat_id, fetching=fetching)
    kb = build_keyboard(chat_id)

    # メニューは常に「編集」優先。編集できなければ新規送信。
    try:
        if u.callback_query and u.callback_query.message:
            await u.callback_query.edit_message_text(text, reply_markup=kb)
        else:
            # /start などは新規メッセージ
            await c.bot.send_message(chat_id, text, reply_markup=kb)
    except Exception as e:
        log.info("edit failed -> send new: %s", e)
        await c.bot.send_message(chat_id, text, reply_markup=kb)

async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = u.effective_chat.id
    # 起動直後は“直近キャッシュ or 直取り”で描画
    if not get_last(chat_id).status:
        status, _ = await fetch_status()
        if status:
            set_last(chat_id, status, now_jp_str())
    await send_or_edit_menu(u, c, fetching=False)

async def msg_start_ja(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    # 「スタート」「開始」にも反応
    await cmd_start(u, c)

async def on_toggle(u: Update, c: ContextTypes.DEFAULT_TYPE, to_on: bool) -> None:
    q = u.callback_query
    await q.answer()  # 先に押下応答
    chat_id = u.effective_chat.id
    set_on(chat_id, to_on)

    if to_on:
        # ONに切り替えたら即取得して反映
        await send_or_edit_menu(u, c, fetching=True)
        status, _ = await fetch_status()
        if status:
            set_last(chat_id, status, now_jp_str())
        else:
            # 取得失敗でも時刻は更新して「失敗」を表示し、次回ポーリングで回復
            set_last(chat_id, "取得失敗", now_jp_str())
        await send_or_edit_menu(u, c, fetching=False)
    else:
        # OFFは取得せず、手元の最新キャッシュで表示だけ更新
        await send_or_edit_menu(u, c, fetching=False)

async def on_get_now(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    q = u.callback_query
    await q.answer()
    chat_id = u.effective_chat.id

    # 1) スピナー表示に編集
    await send_or_edit_menu(u, c, fetching=True)
    # 2) 取得（成否に関わらず時刻は更新して再描画＝毎回“更新感”が出る）
    status, _ = await fetch_status()
    if status:
        set_last(chat_id, status, now_jp_str())
    else:
        # 失敗時は内容は触らず時刻だけ更新（「取得できていません」を回避したいならここで文言をセット）
        last = get_last(chat_id)
        set_last(chat_id, last.status or "取得失敗", now_jp_str())

    await send_or_edit_menu(u, c, fetching=False)

# ====== 通知ジョブ ======
async def poll_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # 購読者にだけ監視をかける
    if not SUBSCRIBERS:
        return
    status, _ = await fetch_status()
    if not status:
        log.info("poll: fetched=None")
        return

    # 変化があったチャットにだけ新規メッセージで通知
    for chat_id in list(SUBSCRIBERS):
        last = get_last(chat_id)
        if status != last.status:
            set_last(chat_id, status, now_jp_str())
            text = f"【更新】王子店ダーツ: {status}（{now_jp_str()}）\n{URL}"
            try:
                await ctx.bot.send_message(chat_id, text, disable_web_page_preview=False)
            except Exception as e:
                log.warning("send failed %s: %s", chat_id, e)
        else:
            # 変化なし：何もしない（メニューは「今すぐ取得」で更新可能）
            pass

# ====== アプリ ======
def build_app() -> Application:
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.Regex(r"^(スタート|開始)$"), msg_start_ja))

    app.add_handler(CallbackQueryHandler(lambda u, c: on_toggle(u, c, True), pattern="^toggle_on$"))
    app.add_handler(CallbackQueryHandler(lambda u, c: on_toggle(u, c, False), pattern="^toggle_off$"))
    app.add_handler(CallbackQueryHandler(on_get_now, pattern="^get_now$"))

    app.job_queue.run_repeating(poll_job, interval=CHECK_INTERVAL_SEC, first=10)
    return app

def main() -> None:
    app = build_app()
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
