# -*- coding: utf-8 -*-
"""
快活クラブ 王子店『ダーツ』空席ウォッチ（Telegram版・Koyeb対応）
- 編集型メニュー（通知ON/OFF／今すぐ取得）
- 2分ごと監視、状態が変われば新規メッセージで通知
- 席ごとの「推定入室時刻」を表示（集計値の増減から割付）
- 環境変数:
    BOT_TOKEN / TELEGRAM_BOT_TOKEN / TG_BOT_TOKEN / TELEGRAM_TOKEN
    SHOP_URL
    SEATS_TOTAL              (既定 4)  … ダーツ台の数
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Dict, List, Optional, Set, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

SEATS_TOTAL = int(os.getenv("SEATS_TOTAL", "4") or "4")

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

# ===================== 状態 =====================
@dataclass
class Seat:
    occupied: bool = False
    entered_at: Optional[datetime] = None  # 推定入室時刻

@dataclass
class BotState:
    subs: Set[int]
    last_status_text: Optional[str]           # サイトから取った文字列（例: 満席 / 残2席）
    last_remain: Optional[int]                # 直近の残席数（0=満席）
    since_full_at: Optional[datetime]         # 直近で満席になった時刻
    seats: List[Seat]                         # 台ごとの占有状態
    menu_msg_ids: Dict[int, int]              # chat_id → message_id
    job_lock: asyncio.Lock                    # 監視ジョブ排他

STATE = BotState(
    subs=set(),
    last_status_text=None,
    last_remain=None,
    since_full_at=None,
    seats=[Seat() for _ in range(SEATS_TOTAL)],
    menu_msg_ids={},
    job_lock=asyncio.Lock(),
)

# ===================== 永続化（購読） =====================
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

# ===================== テキスト整形 =====================
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
        # 例: 「ダーツ 満席」 or 「ダーツ 残 2 席」
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

# ===================== 解析・入室割付 =====================
RE_REMAIN = re.compile(r"残\s*(\d+)\s*席")

def parse_remain(status_text: str) -> Optional[int]:
    if not status_text:
        return None
    if "満席" in status_text:
        return 0
    m = RE_REMAIN.search(status_text)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None

def _assign_entries(delta_newly_occupied: int, ts: datetime) -> None:
    """残席が減った（=入室があった）分だけ、空いている台に入室時刻を割り当て"""
    for seat in STATE.seats:
        if delta_newly_occupied <= 0:
            break
        if not seat.occupied:
            seat.occupied = True
            seat.entered_at = ts
            delta_newly_occupied -= 1

def _release_exits(delta_freed: int) -> None:
    """残席が増えた（=退室があった）分だけ、占有台を開放。古い入室から順に解放"""
    occupied = [s for s in STATE.seats if s.occupied]
    occupied.sort(key=lambda s: s.entered_at or now_dt())  # 古い順
    for seat in occupied:
        if delta_freed <= 0:
            break
        seat.occupied = False
        seat.entered_at = None
        delta_freed -= 1

def apply_new_status(status_text: str) -> None:
    now = now_dt()
    new_remain = parse_remain(status_text)
    if new_remain is None:
        # 不明 → 何もしない（表示は更新）
        STATE.last_status_text = status_text
        return

    # 初期化ケース
    if STATE.last_remain is None:
        need_occupied = SEATS_TOTAL - new_remain
        for i in range(SEATS_TOTAL):
            STATE.seats[i].occupied = i < need_occupied
            STATE.seats[i].entered_at = None if not STATE.seats[i].occupied else None  # 初期は不明
        STATE.since_full_at = now if new_remain == 0 else None
        STATE.last_remain = new_remain
        STATE.last_status_text = status_text
        return

    # 遷移差分
    old = STATE.last_remain
    if new_remain < old:
        # 入室（残が減る）
        _assign_entries(old - new_remain, now)
    elif new_remain > old:
        # 退室（残が増える）
        _release_exits(new_remain - old)

    # フルのトグル管理
    if new_remain == 0 and (STATE.since_full_at is None):
        STATE.since_full_at = now
    if new_remain > 0:
        STATE.since_full_at = None

    STATE.last_remain = new_remain
    STATE.last_status_text = status_text

def seats_snapshot_lines() -> str:
    """席ごとの表示テキスト"""
    lines = []
    for idx, seat in enumerate(STATE.seats, start=1):
        if seat.occupied:
            ts = seat.entered_at.strftime("%H:%M") if seat.entered_at else "時刻不明"
            lines.append(f"・台{idx}: 入室 {ts}")
        else:
            lines.append(f"・台{idx}: 空き")
    return "\n".join(lines)

# ===================== メニュー =====================
def build_menu_text(is_on: bool) -> str:
    on_line = "現在: 🟢 通知ON" if is_on else "現在: 🔴 通知OFF"
    status = STATE.last_status_text or "取得不可"
    status_line = f"現在のダーツ: {status}（{now_jp()}）"
    seats_lines = seats_snapshot_lines()
    return (
        "快活クラブ『ダーツ』空席ウォッチ。\n"
        "下のボタンで通知ON/OFFの切替や、今すぐ取得ができます。\n"
        f"{on_line}\n{status_line}\n\n"
        "入室時刻（推定）:\n"
        f"{seats_lines}"
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
        # 古いメッセージが編集できない等 → 新規で貼り直す
        sent = await c.bot.send_message(chat_id, text, reply_markup=markup)
        STATE.menu_msg_ids[chat_id] = sent.message_id
        log.info("show_or_update_menu: fallback new message due to %s", e)

# ===================== ハンドラ =====================
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await show_or_update_menu(u.effective_chat.id, c)

async def maybe_menu(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    """『スタート』『メニュー』を含むメッセージに反応（スペース・句読点混入OK）"""
    txt = (u.message.text or "").replace(" ", "").replace("　", "")
    if ("スタート" in txt) or ("メニュー" in txt) or ("開始" in txt):
        await show_or_update_menu(u.effective_chat.id, c)

async def on_toggle(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    q = u.callback_query
    await q.answer()
    chat_id = q.message.chat_id  # type: ignore

    if chat_id in STATE.subs:
        # OFF: 取得はせず、最後の値で表示のみ更新
        STATE.subs.discard(chat_id)
        save_subs()
        await show_or_update_menu(chat_id, c)
    else:
        # ON: 直ちに取得して反映
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
        # 変化があれば通知
        prev = STATE.last_status_text
        apply_new_status(status)
        if prev != STATE.last_status_text:
            text = (
                f"【更新】王子店ダーツ: {STATE.last_status_text}（{now_jp()}）\n"
                "入室時刻（推定）:\n"
                f"{seats_snapshot_lines()}\n{URL}"
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
    # 日本語テキストでの呼び出しを緩く拾う
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), maybe_menu))
    app.add_handler(CallbackQueryHandler(on_toggle, pattern="^toggle$"))
    app.add_handler(CallbackQueryHandler(on_fetch, pattern="^fetch$"))
    app.job_queue.run_repeating(poll_job, interval=CHECK_INTERVAL_SEC, first=5)
    return app

def main() -> None:
    token = get_token_from_env()
    if not token:
        log.error("BOT_TOKEN が未設定です。Koyebの環境変数に BOT_TOKEN を設定してください。")
        sys.exit(1)
    app = build_app(token)
    log.info("Bot starting… URL=%s SEATS_TOTAL=%s", URL, SEATS_TOTAL)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()