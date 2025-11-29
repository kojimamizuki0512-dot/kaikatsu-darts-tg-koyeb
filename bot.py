# -*- coding: utf-8 -*-
"""
快活クラブ 王子店『ダーツ』空席ウォッチ（Telegram + Koyeb）

機能:
- 2分ごとに空席監視（変更があれば“新規メッセージ”で通知）
- メニューは常に「編集」で更新（ON/OFFトグル / 今すぐ取得）
- 通知ONにした瞬間、最新の空席状況を即取得してメニューへ反映
- 通知OFFにした瞬間は新規取得せず、直近の結果だけ表示
- tzdataが無い環境でもJSTで動作（ZoneInfo→timezoneにフェイルバック）
- 環境変数 BOT_TOKEN（または TELEGRAM_BOT_TOKEN / TG_BOT_TOKEN）、SHOP_URL、CHECK_INTERVAL_SEC(既定120)

依存:
  python-telegram-bot[job-queue]==20.*
  playwright>=1.45
  tzdata (任意。無くてもJSTにフォールバック)
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
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------- ログ ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("kaikatsu-bot")

# ---------------- タイムゾーン（安全フォールバック） ----------------
try:
    from zoneinfo import ZoneInfo  # py3.9+
    try:
        TZ = ZoneInfo("Asia/Tokyo")
    except Exception:
        TZ = timezone(timedelta(hours=9), name="JST")
except Exception:
    TZ = timezone(timedelta(hours=9), name="JST")


def now_jp() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


# ---------------- 環境変数 ----------------
def _env(*names: str) -> str | None:
    for n in names:
        v = os.getenv(n)
        if v:
            log.info("ENV %s detected", n)
            return v
    return None


BOT_TOKEN = _env("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN")
if not BOT_TOKEN:
    log.error("BOT_TOKEN が未設定です。Koyebの環境変数に BOT_TOKEN（または TELEGRAM_BOT_TOKEN / TG_BOT_TOKEN）を設定してください。")
    sys.exit(1)

SHOP_URL = os.getenv(
    "SHOP_URL",
    "https://www.kaikatsu.jp/shop/detail/vacancy.html?store_code=20328",  # 王子店デフォ
)
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "120"))
SUBS_FILE = os.getenv("SUBS_FILE", "subs.json")

# ---------------- サブスクライブ保存 ----------------
def load_subs() -> set[int]:
    try:
        with open(SUBS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_subs(s: set[int]) -> None:
    try:
        with open(SUBS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(list(s)), f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("save_subs: %s", e)


SUBSCRIBERS: set[int] = load_subs()

# ---------------- 状態共有 ----------------
@dataclass
class Snapshot:
    status: str | None  # "満席" / "残 2 席" / None
    checked_at: datetime | None  # TZ付き


LAST: Snapshot = Snapshot(status=None, checked_at=None)

# Playwrightの競合防止
SCRAPE_LOCK = asyncio.Lock()

# ---------------- 解析ユーティリティ ----------------
_Z2H = str.maketrans("０１２３４５６７８９", "0123456789")


def norm_spaces(s: str) -> str:
    s = s.translate(_Z2H)
    return re.sub(r"[\u3000\t ]+", " ", s)


async def _scrape_once() -> tuple[str | None, str | None]:
    """1回スクレイプ: (status, snippet/err)"""
    from playwright.async_api import async_playwright  # 遅延import

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            ctx = await browser.new_context(
                locale="ja-JP",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"
                ),
                java_script_enabled=True,
            )
            page = await ctx.new_page()
            await page.goto(SHOP_URL, wait_until="domcontentloaded", timeout=45_000)

            # Cookie同意類を可能なら閉じる
            for sel in ["#onetrust-accept-btn-handler", ".btn-accept", "button.accept"]:
                try:
                    await page.locator(sel).click(timeout=1000)
                    break
                except Exception:
                    pass

            await page.wait_for_timeout(1200)
            text = await page.evaluate("document.body.innerText")
            await browser.close()

        t = norm_spaces(text)
        pat = re.compile(r"(満席|残\s*\d+\s*席(?:以上)?)")
        lines = t.splitlines()

        for i, ln in enumerate(lines):
            if "ダーツ" in ln:
                m = pat.search(ln)
                if m:
                    return m.group(1), norm_spaces(ln)[:200]
                ctx2 = " ".join(lines[i : i + 3])
                m = pat.search(ctx2)
                if m:
                    return m.group(1), norm_spaces(ctx2)[:200]

        m = re.search(r"ダーツ.*?(満席|残\s*\d+\s*席(?:以上)?)", t, re.S)
        if m:
            return m.group(1), norm_spaces(t)[:300]

        return None, norm_spaces(t)[:700]
    except Exception as e:
        return None, f"error: {e}\n{traceback.format_exc(limit=1)}"


async def fetch_status(with_lock: bool = True) -> Snapshot:
    """状態取得（Lockで多重実行防止）。失敗でも時刻は入れる。"""

    async def _run() -> Snapshot:
        status, _ = await asyncio.wait_for(_scrape_once(), timeout=60)
        ts = datetime.now(TZ)
        return Snapshot(status=status, checked_at=ts)

    if with_lock:
        async with SCRAPE_LOCK:
            return await _run()
    return await _run()

# ---------------- メニュー ----------------
def keyboard(subscribed: bool) -> InlineKeyboardMarkup:
    toggle_text = "⛔ 通知OFF" if subscribed else "✅ 通知ON"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(toggle_text, callback_data="TOGGLE")],
            [InlineKeyboardButton("🔄 今すぐ取得", callback_data="REFRESH")],
        ]
    )


def build_menu_text(subscribed: bool, last: Snapshot) -> str:
    flag = "🟢 通知ON" if subscribed else "🔴 通知OFF"
    header = (
        "快活クラブ『ダーツ』空席ウォッチ。下のボタンで通知ON/OFFの切替や、今すぐ取得ができます。"
        f"\n現在: {flag}\n"
    )
    if last and last.checked_at:
        checked = last.checked_at.strftime("%Y-%m-%d %H:%M:%S")
        body = f"\n現在のダーツ: {last.status or '取得不可'}（{checked}）"
    else:
        body = "\n現在のダーツ: 取得不可"
    return header + body


async def show_or_update_menu(chat_id: int, c: ContextTypes.DEFAULT_TYPE) -> None:
    subscribed = chat_id in SUBSCRIBERS
    text = build_menu_text(subscribed, LAST)
    kb = keyboard(subscribed)

    msg_id: int | None = c.chat_data.get("menu_message_id")
    if msg_id:
        try:
            m = await c.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=text, reply_markup=kb
            )
            c.chat_data["menu_message_id"] = m.message_id
            return
        except Exception as e:
            log.info("edit_message_text failed; fallback to send: %s", e)

    m = await c.bot.send_message(chat_id, text, reply_markup=kb)
    c.chat_data["menu_message_id"] = m.message_id


async def set_spinner(chat_id: int, c: ContextTypes.DEFAULT_TYPE) -> None:
    subscribed = chat_id in SUBSCRIBERS
    base = (
        "快活クラブ『ダーツ』空席ウォッチ。下のボタンで通知ON/OFFの切替や、今すぐ取得ができます。"
        f"\n現在: {'🟢 通知ON' if subscribed else '🔴 通知OFF'}\n"
        "\n取得中…（最大 ~60 秒）"
    )
    kb = keyboard(subscribed)
    msg_id: int | None = c.chat_data.get("menu_message_id")
    if msg_id:
        try:
            await c.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=base, reply_markup=kb
            )
            return
        except Exception as e:
            log.info("spinner edit failed; fallback to send: %s", e)
    m = await c.bot.send_message(chat_id, base, reply_markup=kb)
    c.chat_data["menu_message_id"] = m.message_id

# ---------------- コマンド ----------------
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await show_or_update_menu(u.effective_chat.id, c)


async def jap_menu(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await show_or_update_menu(u.effective_chat.id, c)


async def cmd_ping(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await u.message.reply_text(f"pong ({now_jp()})")


async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    snap = await fetch_status()
    await u.message.reply_text(f"現在のダーツ: {snap.status or '取得不可'}（{now_jp()}）")


async def cmd_debug(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    async with SCRAPE_LOCK:
        status, hint = await asyncio.wait_for(_scrape_once(), timeout=60)
    msg = f"status={status}\nurl={SHOP_URL}"
    if hint:
        msg += f"\n--- debug ---\n{hint}"
    await u.message.reply_text(msg)

# ---------------- コールバック ----------------
async def on_callback(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    q = u.callback_query
    if not q:
        return
    await q.answer()

    chat_id = q.message.chat_id if q.message else u.effective_chat.id
    data = q.data or ""

    if data == "TOGGLE":
        # 反転
        if chat_id in SUBSCRIBERS:
            SUBSCRIBERS.discard(chat_id)
            save_subs(SUBSCRIBERS)
            await show_or_update_menu(chat_id, c)
        else:
            SUBSCRIBERS.add(chat_id)
            save_subs(SUBSCRIBERS)
            await set_spinner(chat_id, c)
            # グローバル更新は必ず宣言を先頭に
            global LAST
            snap = await fetch_status()
            LAST = snap
            await show_or_update_menu(chat_id, c)

    elif data == "REFRESH":
        await set_spinner(chat_id, c)
        global LAST
        snap = await fetch_status()
        LAST = snap
        await show_or_update_menu(chat_id, c)

# ---------------- 監視ジョブ ----------------
async def poll_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """2分ごと。変化あれば新規メッセージ通知。"""
    try:
        global LAST  # ← 関数の“最初”に置く（参照より前）
        snap = await fetch_status()
        prev = LAST.status
        LAST = snap

        if snap.status and (snap.status != prev):
            text = f"【更新】王子店ダーツ: {snap.status}（{now_jp()}）\n{SHOP_URL}"
            for chat_id in list(SUBSCRIBERS):
                try:
                    await ctx.bot.send_message(chat_id, text)
                except Exception as e:
                    log.warning("notify failed %s: %s", chat_id, e)
    except Exception as e:
        log.error("poll_job error: %s", e)

# ---------------- アプリ構築 ----------------
def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("debug", cmd_debug))

    app.add_handler(MessageHandler(filters.Regex(r"^(スタート|開始|メニュー)$"), jap_menu))
    app.add_handler(CallbackQueryHandler(on_callback))

    app.job_queue.run_repeating(
        poll_job, interval=CHECK_INTERVAL_SEC, first=5, name="poll_job"
    )
    return app


def main() -> None:
    log.info("Bot starting…")
    app = build_app()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
