# -*- coding: utf-8 -*-
"""
快活クラブ 王子店『ダーツ』空席ウォッチ（Telegram版）
/start /on /off /status /debug /ping

安定化ポイント:
- Playwright/Chromium をプロセス内で常駐させ再利用（毎回起動しない）
- --no-sandbox, --disable-dev-shm-usage でコンテナ環境に最適化
- 直列化ロック＋60秒キャッシュ＋リトライ＋長めのタイムアウト
- PTB v20系: JobQueue.run_repeating へは job_kwargs で引数を渡す
"""

from __future__ import annotations
import os, json, re, logging, traceback, time, asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

from telegram import Update
from telegram.ext import ApplicationBuilder, Application, CommandHandler, ContextTypes
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ========= 環境変数 =========
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
URL = os.environ.get("SHOP_URL", "https://www.kaikatsu.jp/shop/detail/vacancy.html?store_code=20328")
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL", "120"))

# ========= ロギング =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("kaikatsu-bot")

# ========= 永続（購読者） =========
SUBS_FILE = "subs.json"
def load_subs() -> set[int]:
    try:
        with open(SUBS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
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

# ========= ユーティリティ =========
_Z2H = str.maketrans("０１２３４５６７８９", "0123456789")
def norm_spaces(s: str) -> str:
    return re.sub(r"[\u3000\t ]+", " ", s.translate(_Z2H))
def now_jp() -> str:
    JST = timezone(timedelta(hours=9))
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")

# ========= Playwright 常駐（シングルトン） =========
SCRAPE_LOCK = asyncio.Lock()
_cache_ts: float = 0.0
_cache_status: Optional[str] = None
_cache_snip: Optional[str] = None
CACHE_TTL = 60  # 秒

_play = None       # Playwright
_browser = None    # Browser
_context = None    # BrowserContext

async def get_context():
    """Chromium を1度だけ起動して使い回す。落ちていたら作り直す。"""
    global _play, _browser, _context
    try:
        if _context:
            return _context
        if _play is None:
            _play = await async_playwright().start()
        if _browser is None:
            _browser = await _play.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-zygote",
                ],
            )
        _context = await _browser.new_context(
            locale="ja-JP",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            java_script_enabled=True,
        )
        return _context
    except Exception as e:
        log.warning("get_context failed: %s", e)
        # クリアして次回再生成
        _play = None
        _browser = None
        _context = None
        raise

async def _scrape_once() -> Tuple[Optional[str], Optional[str]]:
    """Playwrightで1回取得（contextは常駐を使用）。"""
    ctx = await get_context()
    page = await ctx.new_page()
    try:
        page.set_default_navigation_timeout(60000)  # 60s
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        # ほんの少し描画を待つ
        await page.wait_for_timeout(1200)
        # Cookieバナー等があれば閉じる（失敗しても無視）
        for sel in ["#onetrust-accept-btn-handler", ".btn-accept", "button.accept"]:
            try:
                await page.locator(sel).click(timeout=800)
                break
            except Exception:
                pass
        body_text = await page.evaluate("document.body.innerText")
    finally:
        try:
            await page.close()
        except Exception:
            pass

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
    return None, norm_spaces(t)[:500]

async def fetch_status(debug: bool = False, force: bool = False) -> Tuple[Optional[str], Optional[str]]:
    """キャッシュとロック、リトライ付き取得。"""
    global _cache_ts, _cache_status, _cache_snip
    now = time.monotonic()
    if not force and _cache_status is not None and now - _cache_ts < CACHE_TTL:
        return _cache_status, (_cache_snip if debug else None)

    async with SCRAPE_LOCK:
        # 二重取得抑止（待っている間に他スレッドが更新した場合）
        now2 = time.monotonic()
        if not force and _cache_status is not None and now2 - _cache_ts < CACHE_TTL:
            return _cache_status, (_cache_snip if debug else None)

        last_err = None
        for attempt in range(2):  # 軽めにリトライ1回
            try:
                status, snip = await asyncio.wait_for(_scrape_once(), timeout=75)  # 75s
                _cache_ts = time.monotonic()
                _cache_status = status
                _cache_snip = snip
                return status, (snip if debug else None)
            except (PWTimeout, asyncio.TimeoutError) as e:
                last_err = f"timeout: {e}"
                log.warning("scrape timeout (attempt %s): %s", attempt + 1, e)
            except Exception as e:
                last_err = f"error: {e}\n{traceback.format_exc(limit=2)}"
                log.warning("scrape error (attempt %s): %s", attempt + 1, e)
                # コンテキスト壊れている可能性→再生成して次へ
                global _play, _browser, _context
                _play = _browser = _context = None
        return None, last_err

# ========= Telegram コマンド =========
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await u.message.reply_text(
        "王子店『ダーツ』空席ウォッチです。\n"
        "/on で通知ON、/off で通知OFF、/status で現在の状況、/debug は解析用、/ping は疎通チェックです。"
    )

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
    await u.message.reply_text("取得中…（最大 ~60秒）")
    status, _ = await fetch_status(debug=False, force=True)
    await u.message.reply_text(
        f"現在のダーツ: {status}（{now_jp()}）" if status else "取得に失敗しました。少し待って再実行してください。"
    )

async def cmd_debug(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    status, snip = await fetch_status(debug=True, force=True)
    msg = f"status={status}\nURL={URL}"
    if snip:
        msg += f"\n--- debug ---\n{snip}"
    await u.message.reply_text(msg)

# ========= 監視ジョブ =========
async def poll_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global LAST_STATUS
    status, _ = await fetch_status(debug=False, force=False)
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

def build_app() -> Application:
    app = ApplicationBuilder().token(TOKEN).concurrent_updates(False).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("on", cmd_on))
    app.add_handler(CommandHandler("off", cmd_off))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.job_queue.run_repeating(
        poll_job,
        interval=CHECK_INTERVAL_SEC,
        first=10,
        name="poll_job",
        job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30},
    )
    return app

def main() -> None:
    app = build_app()
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
