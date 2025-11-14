# -*- coding: utf-8 -*-
"""
快活クラブ 王子店『ダーツ』空席ウォッチ（Telegram版）
- /start（または「スタート」「メニュー」）でメニュー表示
- ボタン：通知ON/OFFの切替（状態に応じて“次の操作”を表示）/ 今すぐ取得（同メッセージを編集で更新）
- 通知ONにした瞬間は即取得して反映
- 通知OFFにした瞬間は取得せず、最後に取得できた内容のみ表示
- 定期ポーリング（2分おき）で空席状況に変化があれば“新規メッセージ”で通知
- トークンは環境変数 BOT_TOKEN（TELEGRAM_BOT_TOKEN も可）からのみ取得
- tzdataが無い環境でもJST固定オフセットで動作可能
必要パッケージ（参考）:
  pip install "python-telegram-bot[job-queue]"==20.7 playwright==1.47.0
  python -m playwright install chromium
"""

from __future__ import annotations
import os
import sys
import json
import re
import asyncio
import traceback
import logging
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Set
from datetime import datetime, timezone, timedelta

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ===== タイムゾーン（tzdata が無い環境でも動くフォールバック） =====
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Tokyo")
except Exception:
    TZ = timezone(timedelta(hours=9), name="JST")

def now_jp() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

# ===== 設定 =====
URL  = "https://www.kaikatsu.jp/shop/detail/vacancy.html?store_code=20328"  # 王子店 空席ページ
CHECK_INTERVAL_SEC = 120
SUBS_FILE  = "subs.json"   # 通知ONユーザ保存
STATE_FILE = "state.json"  # 直近の取得結果保存（last_status, last_checked_at）

# ===== ログ =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kaikatsu-bot")

# ===== トークンを環境変数から取得・検証 =====
def read_bot_token() -> str:
    t = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    bad = {"REPLACE_ME", "<PUT_YOUR_TOKEN>", ""}
    if (not t) or (t in bad) or (" " in t):
        log.critical("❌ BOT_TOKEN が未設定/不正です。Koyeb の環境変数(Secret推奨)に 'BOT_TOKEN' を設定してください。")
        sys.exit(2)
    return t

TOKEN = read_bot_token()

# ===== JSON永続化 =====
def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _write_json(path: str, obj) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("write_json %s: %s", path, e)

def load_subs() -> Set[int]:
    data = _read_json(SUBS_FILE, [])
    return set(int(x) for x in data)

def save_subs(s: Set[int]) -> None:
    _write_json(SUBS_FILE, list(s))

def load_state() -> Dict[str, str]:
    # 例: {"last_status": "満席" or "残1席", "last_checked_at": "YYYY-MM-DD HH:MM:SS"}
    return _read_json(STATE_FILE, {})

def save_state(status: Optional[str]) -> None:
    state = {
        "last_status": status or "",
        "last_checked_at": now_jp(),
    }
    _write_json(STATE_FILE, state)

SUBSCRIBERS: Set[int] = load_subs()
STATE: Dict[str, str] = load_state()  # 起動時に直近状態を復元（なくてもOK）

# ===== Playwrightでの取得 =====
from playwright.async_api import async_playwright

_Z2H = str.maketrans("０１２３４５６７８９", "0123456789")

def _norm_spaces(s: str) -> str:
    s = s.translate(_Z2H)
    return re.sub(r"[\u3000\t ]+", " ", s)

async def _scrape_once() -> Tuple[Optional[str], Optional[str]]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(
            locale="ja-JP",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            java_script_enabled=True,
        )
        page = await ctx.new_page()
        await page.goto(URL, wait_until="domcontentloaded", timeout=45_000)

        # Cookieバナー等があれば閉じる（失敗は無視）
        for sel in ["#onetrust-accept-btn-handler", ".btn-accept", "button.accept"]:
            try:
                await page.locator(sel).click(timeout=1000)
                break
            except Exception:
                pass

        await page.wait_for_timeout(1200)

        body_text = await page.evaluate("document.body.innerText")
        await browser.close()

    t = _norm_spaces(body_text)
    pat = re.compile(r"(満席|残\s*\d+\s*席(?:以上)?)")
    lines = t.splitlines()

    for i, ln in enumerate(lines):
        if "ダーツ" in ln:
            m = pat.search(ln)
            if m:
                return m.group(1), _norm_spaces(ln)[:200]
            ctx = " ".join(lines[i:i+3])
            m = pat.search(ctx)
            if m:
                return m.group(1), _norm_spaces(ctx)[:200]

    m = re.search(r"ダーツ.*?(満席|残\s*\d+\s*席(?:以上)?)", t, re.S)
    if m:
        return m.group(1), _norm_spaces(t)[:300]

    return None, _norm_spaces(t)[:600]

async def fetch_status() -> Tuple[Optional[str], Optional[str]]:
    """
    成功: (status文字列, デバッグ用スニペット)
    失敗: (None, 解析ヒント)
    """
    try:
        # 1回目
        return await asyncio.wait_for(_scrape_once(), timeout=50)
    except Exception as e1:
        # 2回目（軽めのリトライ）
        log.warning("fetch retry: %s", e1)
        try:
            await asyncio.sleep(1.2)
            return await asyncio.wait_for(_scrape_once(), timeout=50)
        except Exception as e2:
            err = f"error: {e2}\n{traceback.format_exc(limit=2)}"
            return None, err

# ===== UI（テキスト＆ボタン） =====
def is_on(chat_id: int) -> bool:
    return chat_id in SUBSCRIBERS

def current_status_text() -> str:
    last = STATE.get("last_status") or "—"
    ts   = STATE.get("last_checked_at") or now_jp()
    return f"現在のダーツ: {last}（{ts}）"

def menu_text(chat_id: int) -> str:
    on = is_on(chat_id)
    on_line = "現在: 🟢 通知ON" if on else "現在: 🔴 通知OFF"
    return (
        "快活クラブ『ダーツ』空席ウォッチ。\n"
        "下のボタンで通知ON/OFFの切替や、今すぐ取得ができます。\n"
        f"{on_line}\n"
        f"{current_status_text()}"
    )

def spinner_text(chat_id: int) -> str:
    on = is_on(chat_id)
    on_line = "現在: 🟢 通知ON" if on else "現在: 🔴 通知OFF"
    return (
        "快活クラブ『ダーツ』空席ウォッチ。\n"
        "下のボタンで通知ON/OFFの切替や、今すぐ取得ができます。\n"
        f"{on_line}\n"
        "⏳ 取得中…"
    )

def build_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    on = is_on(chat_id)
    # ボタンは「次の操作」を表示：ON中は「通知OFF」、OFF中は「通知ON」
    toggle_label = "⛔ 通知OFF" if on else "🟢 通知ON"
    kb = [
        [InlineKeyboardButton(text=toggle_label, callback_data="toggle")],
        [InlineKeyboardButton(text="🔄 今すぐ取得", callback_data="refresh")],
    ]
    return InlineKeyboardMarkup(kb)

# ===== ハンドラ =====
async def show_menu(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = u.effective_chat.id
    await u.effective_message.reply_text(
        text=menu_text(chat_id),
        reply_markup=build_keyboard(chat_id),
        disable_web_page_preview=True,
    )

async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await show_menu(u, c)

async def on_toggle(cbq, c: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = cbq.message.chat_id
    turned_on = None

    if is_on(chat_id):
        # OFFにする（取得はしない）
        SUBSCRIBERS.discard(chat_id)
        save_subs(SUBSCRIBERS)
        turned_on = False
        await cbq.message.edit_text(
            text=menu_text(chat_id),
            reply_markup=build_keyboard(chat_id),
            disable_web_page_preview=True,
        )
    else:
        # ONにする（即取得して反映）
        SUBSCRIBERS.add(chat_id)
        save_subs(SUBSCRIBERS)
        turned_on = True

        # スピナー表示 → 取得 → 反映
        await cbq.message.edit_text(
            text=spinner_text(chat_id),
            reply_markup=build_keyboard(chat_id),
            disable_web_page_preview=True,
        )
        status, _ = await fetch_status()
        if status:
            STATE["last_status"] = status
            STATE["last_checked_at"] = now_jp()
            save_state(status)

        await cbq.message.edit_text(
            text=menu_text(chat_id),
            reply_markup=build_keyboard(chat_id),
            disable_web_page_preview=True,
        )

    await cbq.answer("通知をONにしました" if turned_on else "通知をOFFにしました")

async def on_refresh(cbq, c: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = cbq.message.chat_id
    # スピナー表示
    await cbq.message.edit_text(
        text=spinner_text(chat_id),
        reply_markup=build_keyboard(chat_id),
        disable_web_page_preview=True,
    )
    # 取得→反映
    status, _ = await fetch_status()
    if status:
        STATE["last_status"] = status
        STATE["last_checked_at"] = now_jp()
        save_state(status)

    await cbq.message.edit_text(
        text=menu_text(chat_id),
        reply_markup=build_keyboard(chat_id),
        disable_web_page_preview=True,
    )
    await cbq.answer("更新しました")

async def cbq_handler(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    cbq = u.callback_query
    data = cbq.data or ""
    try:
        if data == "toggle":
            await on_toggle(cbq, c)
        elif data == "refresh":
            await on_refresh(cbq, c)
        else:
            await cbq.answer("未対応の操作です", show_alert=False)
    except Exception as e:
        log.exception("callback error: %s", e)
        try:
            await cbq.answer("エラーが発生しました", show_alert=True)
        except Exception:
            pass

# ===== 定期ジョブ：変化時に新規メッセージで通知 =====
LAST_STATUS_MEM: Optional[str] = STATE.get("last_status") or None

async def poll_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global LAST_STATUS_MEM
    status, _ = await fetch_status()
    if not status:
        log.info("poll: fetched=None")
        return

    if status != LAST_STATUS_MEM:
        LAST_STATUS_MEM = status
        STATE["last_status"] = status
        STATE["last_checked_at"] = now_jp()
        save_state(status)
        text = f"【更新】王子店ダーツ: {status}（{STATE['last_checked_at']}）\n{URL}"
        # 失敗しても他ユーザは続行
        for chat_id in list(SUBSCRIBERS):
            try:
                await ctx.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
            except Exception as e:
                log.warning("send failed %s: %s", chat_id, e)

# ===== アプリ構築 =====
def build_app() -> Application:
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    # 日本語トリガーでも同じメニューを出す
    app.add_handler(MessageHandler(filters.Regex(r"^(スタート|メニュー)$"), show_menu))

    app.add_handler(CallbackQueryHandler(cbq_handler))

    # ジョブ（2分ごと）
    app.job_queue.run_repeating(poll_job, interval=CHECK_INTERVAL_SEC, first=10)
    return app

def main() -> None:
    app = build_app()
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
