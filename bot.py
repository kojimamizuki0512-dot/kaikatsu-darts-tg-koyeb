# -*- coding: utf-8 -*-
"""
快活クラブ 王子店『ダーツ』空席ウォッチ（Telegram版, Koyeb向け）
- 2分ごとに空席状況を監視（変更があれば新規メッセージで通知）
- チャット内メニューは常に「編集」で更新（ON/OFFトグル / 今すぐ取得）
- 通知ONに切り替えたときは最新の空席状況も即取得して反映
- 通知OFFに切り替えたときは新規取得せず、これまでの最新結果だけ表示
- タイムゾーンは tzdata が無くても落ちないように JST へ確実フォールバック
- 環境変数：SHOP_URL / BOT_TOKEN（TELEGRAM_BOT_TOKEN / TG_BOT_TOKEN も可）

依存：
  python-telegram-bot[job-queue]==20.*
  playwright>=1.45
  tzdata（任意だがあるとZoneInfoが使える）
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

# ========== ログ設定 ==========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("kaikatsu-bot")

# ========== タイムゾーン ==========
# tzdataが無い環境（極小コンテナ等）でも落ちないように安全フォールバック
try:
    from zoneinfo import ZoneInfo  # py3.9+
    try:
        TZ = ZoneInfo("Asia/Tokyo")
    except Exception:
        TZ = timezone(timedelta(hours=9), name="JST")
except Exception:
    TZ = timezone(timedelta(hours=9), name="JST")


def now_jp() -> str:
    """JSTで現在時刻の文字列を返す"""
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


# ========== 設定（環境変数） ==========
def _env(*names: str) -> str | None:
    """複数候補から最初に見つかった環境変数を返す"""
    for n in names:
        v = os.getenv(n)
        if v:
            log.info("ENV %s is set", n)
            return v
    return None


BOT_TOKEN = _env("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN")
if not BOT_TOKEN:
    log.error("BOT_TOKEN が未設定です。Koyebの環境変数に BOT_TOKEN（または TELEGRAM_BOT_TOKEN）を設定してください。")
    sys.exit(1)

SHOP_URL = os.getenv(
    "SHOP_URL",
    "https://www.kaikatsu.jp/shop/detail/vacancy.html?store_code=20328",  # 王子店（デフォルト）
)

CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "120"))
SUBS_FILE = os.getenv("SUBS_FILE", "subs.json")

# ========== サブスクライブ保存 ==========
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

# ========== 監視状態の共有 ==========
@dataclass
class Snapshot:
    status: str | None  # 例: "満席" / "残 2 席" / None(取得不可)
    checked_at: datetime | None  # TZ付き


LAST: Snapshot = Snapshot(status=None, checked_at=None)

# Playwrightの競合を避ける（ポーリング/ボタン/コマンドの同時実行）
SCRAPE_LOCK = asyncio.Lock()


# ========== HTML解析ユーティリティ ==========
_Z2H = str.maketrans("０１２３４５６７８９", "0123456789")


def norm_spaces(s: str) -> str:
    s = s.translate(_Z2H)
    return re.sub(r"[\u3000\t ]+", " ", s)


async def _scrape_once() -> tuple[str | None, str | None]:
    """
    1回のスクレイプ実行。成功: (status, snippet) / 失敗: (None, err/ヒント)
    """
    from playwright.async_api import async_playwright  # 遅延importで起動軽量化

    snippet = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
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

            # Cookie同意などがあれば可能な範囲で閉じる
            for sel in ["#onetrust-accept-btn-handler", ".btn-accept", "button.accept"]:
                try:
                    await page.locator(sel).click(timeout=1000)
                    break
                except Exception:
                    pass

            # 描画待ち
            await page.wait_for_timeout(1200)

            text = await page.evaluate("document.body.innerText")
            await browser.close()

        t = norm_spaces(text)
        pat = re.compile(r"(満席|残\s*\d+\s*席(?:以上)?)")
        lines = t.splitlines()

        # 「ダーツ」を含む行の近傍から拾う
        for i, ln in enumerate(lines):
            if "ダーツ" in ln:
                m = pat.search(ln)
                if m:
                    return m.group(1), norm_spaces(ln)[:200]
                ctx2 = " ".join(lines[i : i + 3])
                m = pat.search(ctx2)
                if m:
                    return m.group(1), norm_spaces(ctx2)[:200]

        # 緩め検索
        m = re.search(r"ダーツ.*?(満席|残\s*\d+\s*席(?:以上)?)", t, re.S)
        if m:
            return m.group(1), norm_spaces(t)[:300]

        return None, norm_spaces(t)[:700]
    except Exception as e:
        return None, f"error: {e}\n{traceback.format_exc(limit=1)}"


async def fetch_status(with_lock: bool = True) -> Snapshot:
    """サイトから状態を取得し、Snapshotを返す（グローバル更新は呼び出し側で行う）"""
    async def _run():
        status, _ = await asyncio.wait_for(_scrape_once(), timeout=60)
        ts = datetime.now(TZ)
        if status:
            return Snapshot(status=status, checked_at=ts)
        return Snapshot(status=None, checked_at=ts)

    if with_lock:
        async with SCRAPE_LOCK:
            return await _run()
    return await _run()


# ========== メニュー（本文＋ボタン） ==========
def keyboard(subscribed: bool) -> InlineKeyboardMarkup:
    """1行1ボタン×2行。上がON/OFFトグル（現在と逆のアクション）/ 下が今すぐ取得"""
    if subscribed:
        toggle_text = "⛔ 通知OFF"
    else:
        toggle_text = "✅ 通知ON"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(text=toggle_text, callback_data="TOGGLE")],
            [InlineKeyboardButton(text="🔄 今すぐ取得", callback_data="REFRESH")],
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
    """メニューを送信or編集。編集できなければ新規送信にフォールバック"""
    subscribed = chat_id in SUBSCRIBERS
    text = build_menu_text(subscribed, LAST)
    kb = keyboard(subscribed)

    # 直近のメニューIDは chat_data に保持（再起動後は編集に失敗→新規送信）
    msg_id: int | None = c.chat_data.get("menu_message_id")
    if msg_id:
        try:
            m = await c.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=kb,
            )
            c.chat_data["menu_message_id"] = m.message_id
            return
        except Exception as e:
            log.info("edit_message_text failed (fallback to send): %s", e)

    m = await c.bot.send_message(chat_id, text, reply_markup=kb)
    c.chat_data["menu_message_id"] = m.message_id


async def set_spinner(chat_id: int, c: ContextTypes.DEFAULT_TYPE) -> None:
    """メニュー文言を『取得中…』に一時的に置き換える（編集）"""
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
                chat_id=chat_id,
                message_id=msg_id,
                text=base,
                reply_markup=kb,
            )
            return
        except Exception as e:
            log.info("spinner edit failed: %s", e)
    # 失敗時は新規
    m = await c.bot.send_message(chat_id, base, reply_markup=kb)
    c.chat_data["menu_message_id"] = m.message_id


# ========== コマンド ==========
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await show_or_update_menu(u.effective_chat.id, c)


async def jap_menu(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    # 「スタート / 開始 / メニュー」でメニュー表示
    await show_or_update_menu(u.effective_chat.id, c)


async def cmd_ping(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await u.message.reply_text(f"pong ({now_jp()})")


async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    # メニューを編集せず、単発で現状を返すデバッグ用
    snap = await fetch_status()
    await u.message.reply_text(
        f"現在のダーツ: {snap.status or '取得不可'}（{now_jp()}）"
    )


async def cmd_debug(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    async with SCRAPE_LOCK:
        status, hint = await asyncio.wait_for(_scrape_once(), timeout=60)
    msg = f"status={status}\nurl={SHOP_URL}"
    if hint:
        msg += f"\n--- debug ---\n{hint}"
    await u.message.reply_text(msg)


# ========== ボタン（コールバック） ==========
async def on_callback(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    q = u.callback_query
    if not q:
        return
    await q.answer()

    chat_id = q.message.chat_id
    data = q.data or ""

    if data == "TOGGLE":
        # 現在と逆にする
        if chat_id in SUBSCRIBERS:
            # OFFへ：保存＆メニュー更新のみ（新規取得はしない）
            SUBSCRIBERS.discard(chat_id)
            save_subs(SUBSCRIBERS)
            await show_or_update_menu(chat_id, c)
        else:
            # ONへ：保存→スピナー→取得→メニュー更新
            SUBSCRIBERS.add(chat_id)
            save_subs(SUBSCRIBERS)
            await set_spinner(chat_id, c)
            snap = await fetch_status()
            # グローバル更新
            global LAST
            LAST = snap
            await show_or_update_menu(chat_id, c)

    elif data == "REFRESH":
        # スピナー→取得→メニュー更新（常に編集）
        await set_spinner(chat_id, c)
        snap = await fetch_status()
        global LAST
        LAST = snap
        await show_or_update_menu(chat_id, c)


# ========== 監視ジョブ ==========
async def poll_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """2分ごとの定期ジョブ。状態変化があればサブスクライバへ新規メッセ通知"""
    try:
        snap = await fetch_status()
        global LAST
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


# ========== アプリ構築 ==========
def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # コマンド
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("debug", cmd_debug))

    # 日本語メニュー合図
    app.add_handler(
        MessageHandler(filters.Regex(r"^(スタート|開始|メニュー)$"), jap_menu)
    )

    # ボタン
    app.add_handler(CallbackQueryHandler(on_callback))

    # 監視ジョブ（同時多重はLockで抑止）
    app.job_queue.run_repeating(
        poll_job,
        interval=CHECK_INTERVAL_SEC,
        first=5,
        name="poll_job",
    )
    return app


def main() -> None:
    log.info("Bot starting…")
    app = build_app()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()