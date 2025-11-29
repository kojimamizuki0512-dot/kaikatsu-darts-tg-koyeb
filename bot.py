# -*- coding: utf-8 -*-
"""
快活クラブ 王子店『ダーツ』空席ウォッチ（Telegram版・Koyeb対応）
- 編集型メニュー（通知ON/OFF／今すぐ取得）
- 2分ごと監視、状態が変われば新規メッセージで通知
- 空き時間予測：満席になってから平均滞在分後 ≒ 次の空きが出やすい時刻を推定
- 環境変数:
    BOT_TOKEN / TELEGRAM_BOT_TOKEN / TG_BOT_TOKEN / TELEGRAM_TOKEN
    SHOP_URL
    AVG_STAY_MIN            (既定 170)
    PREDICT_JITTER_MIN      (既定 30)

必要パッケージ(例):
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

# ===================== 環境設定 =====================
DEFAULT_URL = "https://www.kaikatsu.jp/shop/detail/vacancy.html?store_code=20328"
URL = os.getenv("SHOP_URL", DEFAULT_URL)
CHECK_INTERVAL_SEC = 120
SUBS_FILE = "subs.json"

AVG_STAY_MIN = int(os.getenv("AVG_STAY_MIN", "170") or "170")           # 平均滞在（約2時間50分）
PREDICT_JITTER_MIN = int(os.getenv("PREDICT_JITTER_MIN", "30") or "30") # 表示上の±

# ===================== ロガー =====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kaikatsu-bot")

# ===================== タイムゾーン（堅牢フォールバック） =====================
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Tokyo")
except Exception:
    class _JST(tzinfo):
        def utcoffset(self, dt): return timedelta(hours=9)
        def tzname(self, dt): return "JST"
        def dst(self, dt): return timedelta(0)
    TZ = _JST()  # type: ignore

def now_dt() -> datetime:
    return datetime.now(TZ)

def now_jp() -> str:
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")

def fmt_hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")

# ===================== 状態（global不使用） =====================
@dataclass
class BotState:
    subs: Set[int]
    last_status: Optional[str]            # "満席" / "残 X 席(以上)" 等（テキスト）
    last_kind: Optional[str]              # "full" / "remain" / None
    since_full_at: Optional[datetime]     # 直近で「満席」になった時刻
    menu_msg_ids: Dict[int, int]          # chat_id -> message_id（編集対象）
    job_lock: asyncio.Lock                # 監視ジョブ排他
    spinning: Set[int]                    # スピナー中chat_id

STATE = BotState(
    subs=set(),
    last_status=None,
    last_kind=None,
    since_full_at=None,
    menu_msg_ids={},
    job_lock=asyncio.Lock(),
    spinning=set(),
)

# ===================== 永続化 =====================
def load_subs() -> Set[int]:
    try:
        with open(SUBS_FILE, "r", encoding="utf-8") as f:
            return set(int(x) for x in json.load(f))
    except Exception:
        return set()

def save_subs():
    try:
        with open(SUBS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(list(STATE.subs)), f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("save_subs: %s", e)

STATE.subs = load_subs()

# ===================== 文字整形 =====================
_Z2H = str.maketrans("０１２３４５６７８９", "0123456789")
def norm_spaces(s: str) -> str:
    return re.sub(r"[\u3000\t ]+", " ", s.translate(_Z2H))

# ===================== 取得（Playwright） =====================
from playwright.async_api import async_playwright

async def _scrape_once() -> Tuple[Optional[str], Optional[str]]:
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
            for sel in ("#onetrust-accept-btn-handler", ".btn-accept", "button.accept"):
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
                    return m.group(1), norm_spaces(ln)[:200]
                ctx2 = " ".join(lines[i:i+3])
                m = pat.search(ctx2)
                if m:
                    return m.group(1), norm_spaces(ctx2)[:200]
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

# ===================== 解析・予測 =====================
RE_REMAIN = re.compile(r"残\s*(\d+)\s*席")

def classify(status_text: str) -> Tuple[str, Optional[int]]:
    """
    -> ("full", None) or ("remain", seats) or ("unknown", None)
    """
    if not status_text:
        return "unknown", None
    if "満席" in status_text:
        return "full", None
    m = RE_REMAIN.search(status_text)
    if m:
        try:
            return "remain", int(m.group(1))
        except Exception:
            return "remain", None
    return "unknown", None

def apply_new_status(status_text: str) -> None:
    kind, _ = classify(status_text)
    now = now_dt()
    # 遷移検知
    if kind != STATE.last_kind:
        if kind == "full":
            STATE.since_full_at = now
        elif kind == "remain":
            STATE.since_full_at = None
    STATE.last_kind = kind
    STATE.last_status = status_text

def predict_line() -> str:
    """
    メニューや通知に付ける予測行を返す
    """
    kind, rem = classify(STATE.last_status or "")
    if kind == "remain":
        # 既に空いている
        suffix = f"（残{rem}）" if rem is not None else ""
        return f"予想: いま空いてます{suffix}"
    if kind == "full":
        base = STATE.since_full_at or now_dt()
        eta = base + timedelta(minutes=AVG_STAY_MIN)
        return f"予想: {fmt_hm(eta)} ごろ（±{PREDICT_JITTER_MIN}分）"
    return "予想: 計算不可"

# ===================== UI（メニュー） =====================
def build_menu_text(is_on: bool) -> str:
    on_line = "現在: 🟢 通知ON" if is_on else "現在: 🔴 通知OFF"
    status_line = f"現在のダーツ: {STATE.last_status or '取得不可'}（{now_jp()}）"
    pred_line = predict_line()
    return (
        "快活クラブ『ダーツ』空席ウォッチ。\n"
        "下のボタンで通知ON/OFFの切替や、今すぐ取得ができます。\n"
        f"{on_line}\n{status_line}\n\n{pred_line}"
    )

def build_menu_markup(is_on: bool) -> InlineKeyboardMarkup:
    toggle_label = "⛔ 通知OFFにする" if is_on else "✅ 通知ONにする"
    rows = [
        [InlineKeyboardButton(toggle_label, callback_data="toggle")],
        [InlineKeyboardButton("🔄 今すぐ取得", callback_data="fetch")],
    ]
    return InlineKeyboardMarkup(rows)

async def show_or_update_menu(chat_id: int, c: ContextTypes.DEFAULT_TYPE, *, spin: bool = False) -> None:
    text = "⏳ 取得中…" if spin else build_menu_text(chat_id in STATE.subs)
    markup = build_menu_markup(chat_id in STATE.subs)
    msg_id = STATE.menu_msg_ids.get(chat_id)
    try:
        if msg_id:
            await c.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=markup)
        else:
            sent = await c.bot.send_message(chat_id, text, reply_markup=markup)
            STATE.menu_msg_ids[chat_id] = sent.message_id
    except Exception as e:
        sent = await c.bot.send_message(chat_id, text, reply_markup=markup)
        STATE.menu_msg_ids[chat_id] = sent.message_id
        log.info("show_or_update_menu: fallback new message due to %s", e)

# ===================== ハンドラ =====================
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await show_or_update_menu(u.effective_chat.id, c)

async def jap_menu(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    # 「スタート」「メニュー」「メニューー（長音あり）」に反応
    await show_or_update_menu(u.effective_chat.id, c)

async def on_toggle(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    q = u.callback_query
    await q.answer()
    chat_id = q.message.chat_id  # type: ignore

    if chat_id in STATE.subs:
        # OFF: 取得はしない／最後の値で表示だけ更新
        STATE.subs.discard(chat_id)
        save_subs()
        await show_or_update_menu(chat_id, c)
    else:
        # ON: 即時取得して反映
        STATE.subs.add(chat_id)
        save_subs()
        await show_or_update_menu(chat_id, c, spin=True)
        status, _ = await fetch_status_with_timeout()
        if status:
            apply_new_status(status)
        await show_or_update_menu(chat_id, c)

async def on_fetch(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    q = u.callback_query
    await q.answer()
    chat_id = q.message.chat_id  # type: ignore
    await show_or_update_menu(chat_id, c, spin=True)
    status, _ = await fetch_status_with_timeout()
    if status:
        apply_new_status(status)
    await show_or_update_menu(chat_id, c)

# ===================== 監視ジョブ =====================
async def poll_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if STATE.job_lock.locked():
        return
    async with STATE.job_lock:
        status, _ = await fetch_status_with_timeout()
        log.info("poll: fetched=%s", status)
        if not status:
            return
        if status != STATE.last_status:
            apply_new_status(status)
            text = (
                f"【更新】王子店ダーツ: {STATE.last_status}（{now_jp()}）\n"
                f"{predict_line()}\n{URL}"
            )
            for chat_id in list(STATE.subs):
                try:
                    await ctx.bot.send_message(chat_id, text)
                except Exception as e:
                    log.warning("send failed %s: %s", chat_id, e)

# ===================== Application =====================
def get_token_from_env() -> Optional[str]:
    for k in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN", "TELEGRAM_TOKEN"):
        v = os.getenv(k)
        if v:
            log.info("Using token from env: %s", k)
            return v.strip()
    return None

def build_app(token: str) -> Application:
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(スタート|メニューー*|メニュー)\s*$"), jap_menu))
    app.add_handler(CallbackQueryHandler(on_toggle, pattern="^toggle$"))
    app.add_handler(CallbackQueryHandler(on_fetch, pattern="^fetch$"))
    app.job_queue.run_repeating(poll_job, interval=CHECK_INTERVAL_SEC, first=5)
    return app

def main() -> None:
    token = get_token_from_env()
    if not token:
        log.error("BOT_TOKEN が未設定です。Koyebの環境変数に BOT_TOKEN を設定してください。")
        sys.exit(1)
    try:
        app = build_app(token)
        log.info("Bot starting… URL=%s AVG_STAY_MIN=%s PREDICT_JITTER_MIN=%s",
                 URL, AVG_STAY_MIN, PREDICT_JITTER_MIN)
        app.run_polling(drop_pending_updates=True)
    except Exception as e:
        log.exception("fatal: %s", e)
        sys.exit(1)

if __name__ == "__main__":
    main()