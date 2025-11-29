# -*- coding: utf-8 -*-
"""
快活クラブ 王子店『ダーツ』空席ウォッチ（Telegram版）
- メニューはボタン2段（通知ON/OFFの切替 / 今すぐ取得）
- 通知ONの間は2分ごとに監視、表示が変わったら“新規メッセージ”で通知
- メニューは“編集”で更新（スピナー対応）
- JST表示。tzdataが無くてもJSTにフォールバック
- 予測: 座席数MAX=4、1人あたり平均170分（3h弱）で「次の空き」を推定（±20分幅）

環境変数:
  BOT_TOKEN       Telegram Botトークン
  CHECK_INTERVAL_SEC  監視間隔(秒) 既定=120
  MAX_SEATS       店のダーツ席数 既定=4
  MEAN_MIN        平均滞在(分)   既定=170
  JITTER_MIN      予測幅±(分)    既定=20
"""

from __future__ import annotations
import os, json, logging, re, traceback, asyncio
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# --- JST（tzdata無しでも動くフォールバック） ---
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Tokyo")
except Exception:
    class _JST:
        def utcoffset(self, dt): return timedelta(hours=9)
        def tzname(self, dt): return "JST"
        def dst(self, dt): return timedelta(0)
    TZ = _JST()

from playwright.async_api import async_playwright

# ========= 設定 =========
TOKEN = os.getenv("BOT_TOKEN", "REPLACE_ME")
URL = "https://www.kaikatsu.jp/shop/detail/vacancy.html?store_code=20328"
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "120"))

# 予測パラメータ（必要ならKoyebの環境変数で上書き）
MAX_SEATS = int(os.getenv("MAX_SEATS", "4"))        # 王子店は4席想定
MEAN_MIN  = int(os.getenv("MEAN_MIN",  "170"))      # 平均滞在 ~ 2h50m
JITTER_MIN= int(os.getenv("JITTER_MIN","20"))       # 予測幅 ±20分

SUBS_FILE = "subs.json"   # 購読チャットID（通知ONの人）
STATE_FILE = "state.json" # メニューmsg_id / 直近ステータス / セッション開始時刻群

# ========= ロギング =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kaikatsu-bot")

# ========= 永続データ =========
def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save_json(path: str, data) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("save_json(%s): %s", path, e)

SUBSCRIBERS: set[int] = set(_load_json(SUBS_FILE, []))
STATE: Dict[str, Dict[str, Any]] = _load_json(STATE_FILE, {})
GLOBAL_LAST_STATUS: Optional[str] = None  # 直近の可読ステータス文字列（"満席" / "残1席" など）

# --- sessions: 今在席している人の「開始時刻」のISO文字列リスト（全体共有） ---
def get_sessions() -> List[str]:
    return STATE.setdefault("_sessions", [])

def set_sessions(s: List[str]) -> None:
    STATE["_sessions"] = s
    _save_json(STATE_FILE, STATE)

# ========= ユーティリティ =========
_Z2H = str.maketrans("０１２３４５６７８９", "0123456789")

def norm_spaces(s: str) -> str:
    s = s.translate(_Z2H)
    return re.sub(r"[\u3000\t ]+", " ", s)

def now_jp() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

def iso(dt: datetime) -> str:
    # ISOは辞書順＝時系列順になるので後でsortできる
    return dt.astimezone(TZ).isoformat()

def parse_remaining_from_status(status: Optional[str]) -> Optional[int]:
    if not status:
        return None
    if "満席" in status:
        return 0
    m = re.search(r"残\s*(\d+)\s*席", status)
    if m:
        # 安全のため上限をMAX_SEATSに丸める
        return min(MAX_SEATS, int(m.group(1)))
    return None

def is_on(chat_id: int) -> bool:
    return chat_id in SUBSCRIBERS

def state_of(chat_id: int) -> Dict[str, Any]:
    return STATE.setdefault(str(chat_id), {})

def set_menu_msg_id(chat_id: int, msg_id: int) -> None:
    s = state_of(chat_id)
    s["menu_msg_id"] = msg_id
    _save_json(STATE_FILE, STATE)

def get_menu_msg_id(chat_id: int) -> Optional[int]:
    return state_of(chat_id).get("menu_msg_id")

def set_last_status_for_chat(chat_id: int, status: Optional[str]) -> None:
    s = state_of(chat_id)
    s["last_status"] = status
    _save_json(STATE_FILE, STATE)

def get_last_status_for_chat(chat_id: int) -> Optional[str]:
    return state_of(chat_id).get("last_status")

def build_keyboard(on: bool) -> InlineKeyboardMarkup:
    toggle_label = "🔴 通知OFFにする" if on else "🟢 通知ONにする"
    rows = [
        [InlineKeyboardButton(toggle_label, callback_data="toggle")],
        [InlineKeyboardButton("🔄 今すぐ取得", callback_data="refresh")],
    ]
    return InlineKeyboardMarkup(rows)

# ========= 予測（セッション更新＆次の空き推定） =========
def update_sessions_with_status(new_status: Optional[str]) -> None:
    """可視ステータスから占有数を推定し、sessionsを増減して整合させる。"""
    rem = parse_remaining_from_status(new_status)
    if rem is None:
        return
    target_occ = max(0, min(MAX_SEATS, MAX_SEATS - rem))

    sess = get_sessions()
    curr_occ = len(sess)
    now = datetime.now(TZ)

    if target_occ > curr_occ:
        # 新たに入った人の開始時刻を now として積む（監視間隔ぶんの誤差は許容）
        for _ in range(target_occ - curr_occ):
            sess.append(iso(now))
        set_sessions(sess)
    elif target_occ < curr_occ:
        # 退出者が出た → いちばん古い開始時刻から削る
        if curr_occ > 0:
            sess.sort()  # ISO順=時系列
            # 残すのは“直近で入った人”＝末尾側 target_occ 件
            kept = sess[-target_occ:] if target_occ > 0 else []
            set_sessions(kept)

def prediction_line() -> str:
    """メニューに出す予測行。「いま空きあり」or 時間帯レンジ or ー"""
    sess = get_sessions()
    # セッションがMAX未満 → すでに空きがある
    if len(sess) < MAX_SEATS:
        return "次の空き予想: いま空きあり"

    if not sess:
        return "次の空き予想: ー"

    # “最も早く終わりそう”な人 = 開始が最も古い人
    sess.sort()
    oldest_iso = sess[0]
    try:
        oldest = datetime.fromisoformat(oldest_iso)
    except Exception:
        return "次の空き予想: ー"

    eta = oldest + timedelta(minutes=MEAN_MIN)
    early = (eta - timedelta(minutes=JITTER_MIN)).astimezone(TZ).strftime("%H:%M")
    late  = (eta + timedelta(minutes=JITTER_MIN)).astimezone(TZ).strftime("%H:%M")
    return f"次の空き予想: {early}〜{late} ごろ"

def build_menu_text(on: bool, last_status: Optional[str]) -> str:
    line_state = "現在: 🟢 通知ON" if on else "現在: 🔴 通知OFF"
    line_status = f"現在のダーツ: {last_status or '取得不可'}（{now_jp()}）"
    line_pred = prediction_line()
    return (
        "快活クラブ『ダーツ』空席ウォッチ。\n"
        "下のボタンで通知ON/OFFの切替や、今すぐ取得ができます。\n"
        f"{line_state}\n{line_status}\n{line_pred}"
    )

# ========= 取得＆解析（PlaywrightでJS後の本文を読む） =========
async def _scrape_once() -> Tuple[Optional[str], Optional[str]]:
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
                await page.locator(sel).click(timeout=1000)
                break
            except Exception:
                pass

        await page.wait_for_timeout(1000)
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
            ctx = " ".join(lines[i:i+3])
            m = pat.search(ctx)
            if m:
                return m.group(1), norm_spaces(ctx)[:200]

    m = re.search(r"ダーツ.*?(満席|残\s*\d+\s*席(?:以上)?)", t, re.S)
    if m:
        return m.group(1), norm_spaces(t)[:300]
    return None, norm_spaces(t)[:500]

async def fetch_status() -> Tuple[Optional[str], Optional[str]]:
    try:
        return await asyncio.wait_for(_scrape_once(), timeout=45)
    except asyncio.TimeoutError:
        return None, "timeout"
    except Exception as e:
        hint = f"error: {e}\n{traceback.format_exc(limit=2)}"
        return None, hint

# ========= メニュー描画 =========
async def show_or_update_menu(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, *, spinner: bool = False) -> None:
    on = is_on(chat_id)
    last = get_last_status_for_chat(chat_id) or GLOBAL_LAST_STATUS
    kb = build_keyboard(on)

    msg_id = get_menu_msg_id(chat_id)
    text = build_menu_text(on, last)

    if spinner:
        spin_text = text.replace("現在のダーツ:", "現在のダーツ: ⏳ 取得中…")
        try:
            if msg_id:
                await ctx.bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id, text=spin_text, reply_markup=kb
                )
            else:
                m = await ctx.bot.send_message(chat_id, spin_text, reply_markup=kb)
                set_menu_msg_id(chat_id, m.message_id)
                msg_id = m.message_id
        except Exception as e:
            log.warning("spinner edit failed: %s", e)

    # 最終描画
    try:
        if msg_id:
            await ctx.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=text, reply_markup=kb
            )
        else:
            m = await ctx.bot.send_message(chat_id, text, reply_markup=kb)
            set_menu_msg_id(chat_id, m.message_id)
    except Exception as e:
        log.info("menu edit failed -> send new: %s", e)
        m = await ctx.bot.send_message(chat_id, text, reply_markup=kb)
        set_menu_msg_id(chat_id, m.message_id)

# ========= コマンド/ハンドラ =========
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await show_or_update_menu(u.effective_chat.id, c)

async def jap_menu(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await show_or_update_menu(u.effective_chat.id, c)

async def cb_handler(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    global GLOBAL_LAST_STATUS
    q = u.callback_query
    if not q:
        return
    chat_id = q.message.chat_id
    data = q.data or ""
    await q.answer()

    if data == "toggle":
        if is_on(chat_id):
            # OFFにする：即オフ。最新の取得はしない（直近の値を表示）
            SUBSCRIBERS.discard(chat_id)
            _save_json(SUBS_FILE, list(SUBSCRIBERS))
            await show_or_update_menu(chat_id, c)
        else:
            # ONにする：スピナー→取得→セッション更新→メニュー
            SUBSCRIBERS.add(chat_id)
            _save_json(SUBS_FILE, list(SUBSCRIBERS))
            await show_or_update_menu(chat_id, c, spinner=True)
            status, _ = await fetch_status()
            if status:
                GLOBAL_LAST_STATUS = status
                update_sessions_with_status(status)
                set_last_status_for_chat(chat_id, status)
            await show_or_update_menu(chat_id, c)

    elif data == "refresh":
        await show_or_update_menu(chat_id, c, spinner=True)
        status, _ = await fetch_status()
        if status:
            GLOBAL_LAST_STATUS = status
            update_sessions_with_status(status)
            set_last_status_for_chat(chat_id, status)
        await show_or_update_menu(chat_id, c)

async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = u.effective_chat.id
    await show_or_update_menu(chat_id, c, spinner=True)
    status, _ = await fetch_status()
    if status:
        global GLOBAL_LAST_STATUS
        GLOBAL_LAST_STATUS = status
        update_sessions_with_status(status)
        set_last_status_for_chat(chat_id, status)
    await show_or_update_menu(chat_id, c)

async def cmd_debug(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    status, snip = await fetch_status()
    if status:
        update_sessions_with_status(status)
    msg = f"status={status}\nURL={URL}"
    if snip:
        msg += f"\n--- debug ---\n{snip}"
    await u.message.reply_text(msg)

# ========= 監視ジョブ =========
async def poll_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global GLOBAL_LAST_STATUS
    status, _ = await fetch_status()
    log.info("poll: fetched=%s", status)
    if not status:
        return

    # セッション更新 & 変更検知
    prev = GLOBAL_LAST_STATUS
    update_sessions_with_status(status)
    if status != prev:
        GLOBAL_LAST_STATUS = status
        # 新規通知メッセージに予測も添える
        text = f"【更新】王子店ダーツ: {status}（{now_jp()}）\n{prediction_line()}\n{URL}"
        for chat_id in list(SUBSCRIBERS):
            try:
                await ctx.bot.send_message(chat_id, text)
                set_last_status_for_chat(chat_id, status)
            except Exception as e:
                log.warning("send failed %s: %s", chat_id, e)

# ========= アプリ起動 =========
def build_app() -> Application:
    if not TOKEN or TOKEN == "REPLACE_ME":
        log.error("BOT_TOKEN が未設定です。Koyebの環境変数に BOT_TOKEN を設定してください。")
        raise SystemExit(1)

    app = ApplicationBuilder().token(TOKEN).build()

    # コマンド
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("debug", cmd_debug))
    # 日本語トリガ（「スタート」「メニュー」）
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(スタート|メニュー)$"), jap_menu))
    # ボタン
    app.add_handler(CallbackQueryHandler(cb_handler))

    # 監視ジョブ
    app.job_queue.run_repeating(poll_job, interval=CHECK_INTERVAL_SEC, first=5)
    return app

def main() -> None:
    app = build_app()
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
