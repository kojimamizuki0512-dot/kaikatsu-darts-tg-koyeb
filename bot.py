# -*- coding: utf-8 -*-
"""
快活クラブ 王子店『ダーツ』空席ウォッチ（Telegram版・Koyeb対応）
- /start（日本語: スタート/メニュー）で編集型メニューを表示
- 「通知ON/OFF」トグル: ON時は即時取得して更新、OFF時は取得せず最後の値を表示
- 「今すぐ取得」: 同じメッセージを“編集”して最新を表示（新規メッセージは出さない）
- 2分ごとに監視し、空席状況が変わったら新規メッセージで通知
- グローバル宣言（global）は一切不使用。状態は STATE 辞書に集約。

必要パッケージ(一例):
  pip install "python-telegram-bot[job-queue]"==20.7 playwright==1.47.0 tzdata
  python -m playwright install chromium
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Dict, Optional, Set, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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

# ===================== 基本設定 =====================
URL = "https://www.kaikatsu.jp/shop/detail/vacancy.html?store_code=20328"
CHECK_INTERVAL_SEC = 120
SUBS_FILE = "subs.json"

# ===================== ロガー =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kaikatsu-bot")

# ===================== タイムゾーン（堅牢フォールバック） =====================
try:
    from zoneinfo import ZoneInfo  # py3.9+
    TZ = ZoneInfo("Asia/Tokyo")
except Exception:
    # tzdata が無い・読み込めない環境向けに手製のJST
    class _JST(tzinfo):
        def utcoffset(self, dt): return timedelta(hours=9)
        def tzname(self, dt): return "JST"
        def dst(self, dt): return timedelta(0)
    TZ = _JST()  # type: ignore[assignment]

def now_jp() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

# ===================== 状態（global不使用） =====================
@dataclass
class BotState:
    subs: Set[int]
    last_status: Optional[str]
    menu_msg_ids: Dict[int, int]   # chat_id -> message_id（編集対象）
    job_lock: asyncio.Lock         # 監視ジョブ用ロック
    spinning: Set[int]             # 取得ボタンでスピナー中の chat_id

STATE = BotState(
    subs=set(),
    last_status=None,
    menu_msg_ids={},
    job_lock=asyncio.Lock(),
    spinning=set(),
)

# ===================== 購読データの永続化 =====================
def load_subs() -> Set[int]:
    try:
        with open(SUBS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(int(x) for x in data)
    except Exception:
        return set()

def save_subs():
    try:
        with open(SUBS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(list(STATE.subs)), f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("save_subs: %s", e)

STATE.subs = load_subs()

# ===================== 文字整形ユーティリティ =====================
_Z2H = str.maketrans("０１２３４５６７８９", "0123456789")

def norm_spaces(s: str) -> str:
    s = s.translate(_Z2H)
    return re.sub(r"[\u3000\t ]+", " ", s)

# ===================== Playwrightでの取得 =====================
from playwright.async_api import async_playwright

async def _scrape_once() -> Tuple[Optional[str], Optional[str]]:
    """
    成功: (status文字列, デバッグスニペット)
    失敗: (None, 失敗理由/例外)
    """
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

            # Cookie等のモーダルを雑に潰す
            for sel in ("#onetrust-accept-btn-handler", ".btn-accept", "button.accept"):
                try:
                    await page.locator(sel).click(timeout=800)
                    break
                except Exception:
                    pass

            # 描画待ち
            await page.wait_for_timeout(1200)

            body_text = await page.evaluate("document.body.innerText")
            await browser.close()

        t = norm_spaces(body_text)
        # ダーツ行の近傍から「満席 / 残X席(以上)」を抽出
        pat = re.compile(r"(満席|残\s*\d+\s*席(?:以上)?)")
        lines = t.splitlines()
        for i, ln in enumerate(lines):
            if "ダーツ" in ln:
                m = pat.search(ln)
                if m:
                    return m.group(1), norm_spaces(ln)[:200]
                ctx2 = " ".join(lines[i:i+3])
                m = pat.search(ctx2)
                if m:
                    return m.group(1), norm_spaces(ctx2)[:200]

        # 全体からの緩めマッチ
        m = re.search(r"ダーツ.*?(満席|残\s*\d+\s*席(?:以上)?)", t, re.S)
        if m:
            return m.group(1), norm_spaces(t)[:300]

        return None, "parse_miss"

    except Exception as e:
        return None, f"error: {e}"

async def fetch_status_with_timeout(timeout_sec: int = 45) -> Tuple[Optional[str], Optional[str]]:
    try:
        return await asyncio.wait_for(_scrape_once(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        return None, "timeout"
    except Exception as e:
        return None, f"error: {e}"

# ===================== UI（メニュー） =====================
def build_menu_text(is_on: bool, last_status: Optional[str]) -> str:
    on_line = "現在: 🟢 通知ON" if is_on else "現在: 🔴 通知OFF"
    line_status = f"現在のダーツ: {last_status or '取得不可'}（{now_jp()}）"
    return (
        "快活クラブ『ダーツ』空席ウォッチ。\n"
        "下のボタンで通知ON/OFFの切替や、今すぐ取得ができます。\n"
        f"{on_line}\n{line_status}"
    )

def build_menu_markup(is_on: bool) -> InlineKeyboardMarkup:
    # 「ONのときはオフにするボタン」を出す（逆も同様）＝ユーザーが次にやれる行動を出す
    toggle_label = "⛔ 通知OFFにする" if is_on else "✅ 通知ONにする"
    rows = [
        [InlineKeyboardButton(toggle_label, callback_data="toggle")],
        [InlineKeyboardButton("🔄 今すぐ取得", callback_data="fetch")],
    ]
    return InlineKeyboardMarkup(rows)

async def show_or_update_menu(chat_id: int, c: ContextTypes.DEFAULT_TYPE, *, spin: bool = False) -> None:
    """メニューを新規送信/編集。spin=True のときはスピナーテキストを一時表示。"""
    is_on = chat_id in STATE.subs
    text = "⏳ 取得中…" if spin else build_menu_text(is_on, STATE.last_status)
    markup = build_menu_markup(is_on)

    msg_id = STATE.menu_msg_ids.get(chat_id)
    try:
        if msg_id:
            await c.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=markup,
            )
        else:
            sent = await c.bot.send_message(chat_id, text, reply_markup=markup)
            STATE.menu_msg_ids[chat_id] = sent.message_id
    except Exception as e:
        # 旧メッセージが編集不能/消えている等 → 新規で立て直し
        sent = await c.bot.send_message(chat_id, text, reply_markup=markup)
        STATE.menu_msg_ids[chat_id] = sent.message_id
        log.info("show_or_update_menu: fallback new message due to %s", e)

# ===================== Telegram Handlers =====================
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await show_or_update_menu(u.effective_chat.id, c)

async def jap_menu(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    # 日本語トリガー（スタート/メニュー）
    await show_or_update_menu(u.effective_chat.id, c)

async def on_toggle(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    q = u.callback_query
    await q.answer()
    chat_id = q.message.chat_id  # type: ignore[assignment]

    if chat_id in STATE.subs:
        # OFFにする：取得はしない。最後の値を表示して状態だけ切替
        STATE.subs.discard(chat_id)
        save_subs()
        await show_or_update_menu(chat_id, c)
    else:
        # ONにする：即取得 → 反映
        STATE.subs.add(chat_id)
        save_subs()
        await show_or_update_menu(chat_id, c, spin=True)
        status, _ = await fetch_status_with_timeout()
        if status:
            STATE.last_status = status
        await show_or_update_menu(chat_id, c)

async def on_fetch(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    q = u.callback_query
    await q.answer()
    chat_id = q.message.chat_id  # type: ignore[assignment]

    # スピナー表示 → 取得 → 反映（同一メッセージ編集のみ）
    await show_or_update_menu(chat_id, c, spin=True)
    status, _ = await fetch_status_with_timeout()
    if status:
        STATE.last_status = status
    await show_or_update_menu(chat_id, c)

# ===================== 監視ジョブ =====================
async def poll_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # 多重起動防止（PTBのmax_instances無しでも安全）
    if STATE.job_lock.locked():
        return
    async with STATE.job_lock:
        status, _ = await fetch_status_with_timeout()
        log.info("poll: fetched=%s", status)
        if not status:
            return
        if status != STATE.last_status:
            STATE.last_status = status
            text = f"【更新】王子店ダーツ: {status}（{now_jp()}）\n{URL}"
            for chat_id in list(STATE.subs):
                try:
                    await ctx.bot.send_message(chat_id, text)
                except Exception as e:
                    log.warning("send failed %s: %s", chat_id, e)

# ===================== Application 構築 =====================
def get_token_from_env() -> Optional[str]:
    keys = ["BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN", "TELEGRAM_TOKEN"]
    for k in keys:
        v = os.getenv(k)
        if v:
            log.info("Using token from env: %s", k)
            return v.strip()
    return None

def build_app(token: str) -> Application:
    app = ApplicationBuilder().token(token).build()

    # コマンド
    app.add_handler(CommandHandler("start", cmd_start))
    # 日本語メニュー（/startエイリアス）
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(r"^(スタート|メニュー)$"),
        jap_menu
    ))
    # コールバックボタン
    app.add_handler(CallbackQueryHandler(on_toggle, pattern="^toggle$"))
    app.add_handler(CallbackQueryHandler(on_fetch, pattern="^fetch$"))

    # 2分ごとに監視
    app.job_queue.run_repeating(poll_job, interval=CHECK_INTERVAL_SEC, first=5)

    return app

def main() -> None:
    token = get_token_from_env()
    if not token:
        log.error("BOT_TOKEN が未設定です。Koyebの環境変数に BOT_TOKEN を設定してください。")
        sys.exit(1)

    try:
        app = build_app(token)
        log.info("Bot starting…")
        app.run_polling(drop_pending_updates=True)
    except Exception as e:
        log.exception("fatal: %s", e)
        sys.exit(1)

if __name__ == "__main__":
    main()