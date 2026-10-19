"""Premium subscription Telegram bot.

Flow: user plan chunta hai -> UPI/QR se pay karke screenshot bhejta hai ->
admin approve karta hai -> user ko 1-use invite link milta hai ->
expiry par bot khud channel se hata deta hai.
"""
import asyncio
import html
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pymongo import AsyncMongoClient, ReturnDocument
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("subbot")

# ================================================================ CONFIG
BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
MONGO_DB = os.getenv("MONGO_DB", "subbot")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(",", " ").split() if x.isdigit()}
UPI_ID = os.getenv("UPI_ID", "yourname@upi")
CONTACT_URL = os.getenv("CONTACT_URL", "https://t.me")

IST = ZoneInfo("Asia/Kolkata")
HOUR, DAY = 3600, 86400
REMIND_DAYS = 2  # expiry se itne din pehle reminder

# ---- /availableotts ka reply ----
AVAILABLE_OTTS_TEXT = "📌AVAILABLE OTT's & Services
"Alt Balaji" 🅰️
"Aha" 🔶
"Arey Digital" 🎬
"Amazon Mini Tv" 📺
"Bongo Tv" 🌐
"Dangal-Play" 🟣
"Discovery+" 🎱 
"Jio-Hotstar" 💠
"Jio-Cinema" 🌸
"NETFLIX" 🧧
"Pocket FM" 🀄
"Prime Video" 🔵
"STAGE" 🌀
"SHEMAROO ME" 📽️
"SONY LIV" 🟣
"Sun NXT" 🎃
"ZEE 5" ♈

📌 Total = 17 OTT's 




👾ALL Daily Shows 👾
"TV SHOWS"🌈
"Colors"
"Dangal-Play"
"Jio-Hotstar"
"SonyLivv" ☘️
"Sun NXT"
"ZEE-5"

Example: ↓↓
"Taarak Mehta"
"Bigg Boss"
"New-CiD S03"
"Pushpa Impossible"
"Kumkum Bhagya"
"All Other On-going Shows"

"Weekly Releases Show's ⚡
"Reality Show's"
"Laughter Chef"
"India's Got Talent"
"All Other Shows""

# ---- Plans (price ₹ mein) ----
PLANS = [
    {"id": "60d", "label": "🥇 60 Days", "secs": 60 * DAY, "price": 79},
    {"id": "30d", "label": "🥈 30 Days", "secs": 30 * DAY, "price": 39},
    {"id": "7d", "label": "🥉 7 Days", "secs": 7 * DAY, "price": 19},
    {"id": "3h", "label": "⚜️ 3 Hours", "secs": 3 * HOUR, "price": 9},
]

# ---- Products. channel_env = us channel ki ID jis env variable mein hogi ----
PRODUCTS = {
    "movies": {
        "name": "🎬 Movies Premium",
        "terms": "• Payment non-refundable\n• Sirf personal use\n• Link share karna mana hai",
        "channel_env": "CH_MOVIES",
    },
    "music": {
        "name": "🎧 Music Premium",
        "terms": "• Payment non-refundable\n• Sirf personal use\n• Link share karna mana hai",
        "channel_env": "CH_MUSIC",
    },
}

# Admin "Change duration" ke options
DURATION_CHOICES = [
    (3 * HOUR, "3h"), (7 * DAY, "7d"), (15 * DAY, "15d"), (30 * DAY, "30d"),
    (60 * DAY, "60d"), (90 * DAY, "90d"), (180 * DAY, "180d"), (365 * DAY, "365d"),
]


def get_plan(pid):
    return next((p for p in PLANS if p["id"] == pid), None)


def pname(key):
    return PRODUCTS.get(key, {}).get("name", key)


def channel_of(key):
    v = os.getenv(PRODUCTS[key]["channel_env"], "").strip().strip("\"'").strip()
    try:
        return int(v)
    except ValueError:
        return None


def all_channels():
    return sorted({channel_of(k) for k in PRODUCTS if channel_of(k)})


# ================================================================ DB
client = None
db = None


async def post_init(app):
    global client, db
    client = AsyncMongoClient(MONGO_URI, serverSelectionTimeoutMS=15000)
    db = client[MONGO_DB]
    await client.admin.command("ping")
    log.info("MongoDB connected (db=%s)", MONGO_DB)


async def post_shutdown(app):
    if client is not None:
        await client.close()


async def next_req_id():
    doc = await db.counters.find_one_and_update(
        {"_id": "requests"}, {"$inc": {"seq": 1}},
        upsert=True, return_document=ReturnDocument.AFTER,
    )
    return doc["seq"]


async def get_setting(key):
    r = await db.settings.find_one({"_id": key})
    return r["value"] if r else None


async def set_setting(key, value):
    await db.settings.update_one({"_id": key}, {"$set": {"value": value}}, upsert=True)


async def touch_user(u):
    now = int(time.time())
    await db.users.update_one(
        {"_id": u.id},
        {"$set": {"username": u.username, "full_name": u.full_name, "last_seen": now},
         "$setOnInsert": {"first_seen": now}},
        upsert=True,
    )


# ================================================================ HELPERS
def fmt(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%d %b %Y, %I:%M %p")


def fmt_dur(secs):
    return f"{max(1, secs // HOUR)} Hours" if secs < DAY else f"{secs // DAY} Days"


def fmt_left(secs):
    if secs >= DAY:
        return f"{secs // DAY} din"
    if secs >= HOUR:
        return f"{secs // HOUR} ghante"
    return f"{max(1, secs // 60)} minute"


def esc(s):
    return html.escape(str(s) if s is not None else "")


def is_admin(uid):
    return uid in ADMIN_IDS


async def is_banned(uid):
    if is_admin(uid):
        return False
    return await db.banned.find_one({"_id": uid}) is not None


async def make_invite(context, key, ttl=DAY):
    ch = channel_of(key)
    if not ch:
        raise RuntimeError(f"{PRODUCTS[key]['channel_env']} set nahi hai ya galat hai")
    link = await context.bot.create_chat_invite_link(
        ch, member_limit=1,
        expire_date=datetime.now(timezone.utc) + timedelta(seconds=max(ttl, 60)),
    )
    return link.invite_link


def link_ttl_text(ttl):
    return f"{max(1, ttl // HOUR)} ghante" if ttl >= HOUR else "kuch minute"


async def kick(context, uid, key):
    ch = channel_of(key)
    if not ch:
        return
    try:
        await context.bot.ban_chat_member(ch, uid)
        # unban taaki renew karne par wapas join kar sake
        await context.bot.unban_chat_member(ch, uid, only_if_banned=True)
    except Exception as e:
        log.warning("Kick fail %s/%s: %s", uid, key, e)


# ================================================================ USER SIDE
HOME_TEXT = "👋 <b>Welcome!</b>\n\nNeeche se apna premium plan choose karein 👇"


def home_markup():
    rows = [[InlineKeyboardButton(p["name"], callback_data=f"p:{k}")] for k, p in PRODUCTS.items()]
    rows.append([InlineKeyboardButton("💬 Contact Admin", url=CONTACT_URL)])
    rows.append([InlineKeyboardButton("📋 My Subscriptions", callback_data="my")])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await touch_user(u)
    if await is_banned(u.id):
        await update.effective_message.reply_text("🚫 Aap is bot ko use nahi kar sakte.")
        return
    context.user_data.pop("buying", None)
    await update.effective_message.reply_text(
        HOME_TEXT, reply_markup=home_markup(), parse_mode=ParseMode.HTML
    )


async def availableotts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await touch_user(u)
    if await is_banned(u.id):
        return
    await update.message.reply_text(AVAILABLE_OTTS_TEXT)


async def show_product(q, key):
    p = PRODUCTS[key]
    text = (
        f"🎗 <b>VIP ACCESS PLANS</b> 🎗\n<b>{esc(p['name'])}</b>\n\n"
        f"📜 <b>Terms:</b>\n{esc(p['terms'])}\n\n👇 Apna plan select karein"
    )
    rows = [
        [InlineKeyboardButton(f"{pl['label']} — ₹{pl['price']}", callback_data=f"plan:{key}:{pl['id']}")]
        for pl in PLANS
    ]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)


async def show_payment(q, context, key, pid):
    pl = get_plan(pid)
    if not pl:
        return
    context.user_data["buying"] = {"key": key, "plan": pid}
    has_qr = bool(await get_setting("qr_file_id"))
    text = (
        f"<b>{esc(PRODUCTS[key]['name'])}</b>\n"
        f"📦 Plan: <b>{pl['label']} — ₹{pl['price']}</b>\n\n"
        "<b>Payment karein</b>\n\n"
        f"UPI ID (tap karke copy karein):\n<code>{esc(UPI_ID)}</code>\n\n"
    )
    rows = []
    if has_qr:
        text += "Ya QR scan karein 👇\n\n"
        rows.append([InlineKeyboardButton("QR-CODE", callback_data="qr")])
    text += "Payment ke baad yahin <b>screenshot (photo)</b> bhejein."
    rows.append([InlineKeyboardButton("⬅️ Back / Cancel", callback_data=f"p:{key}")])
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)


async def my_subs_text(user_id):
    now = int(time.time())
    rows = await db.subs.find(
        {"user_id": user_id, "active": True, "expiry_ts": {"$gt": now}}
    ).to_list(None)
    if not rows:
        return "Aapka koi active subscription nahi hai."
    lines = ["<b>📋 Aapke active plans:</b>\n"]
    for r in rows:
        lines.append(f"{esc(pname(r['product']))}\nExpiry: {fmt(r['expiry_ts'])} ({fmt_left(r['expiry_ts'] - now)} baaki)")
    return "\n".join(lines)


async def user_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if await is_banned(q.from_user.id):
        await q.answer("🚫 Aap banned hain", show_alert=True)
        return
    data = q.data

    if data == "qr":
        fid = await get_setting("qr_file_id")
        if not fid:
            await q.answer("QR abhi available nahi hai. UPI se payment karein.", show_alert=True)
            return
        await q.answer()
        cap = "📷 QR scan karke payment karein."
        buying = context.user_data.get("buying")
        if buying:
            pl = get_plan(buying["plan"])
            if pl:
                cap += f"\n{pl['label']} — ₹{pl['price']}"
        cap += "\n\nPayment ke baad yahin screenshot (photo) bhejein."
        await context.bot.send_photo(q.from_user.id, fid, caption=cap)
        return

    await q.answer()
    await touch_user(q.from_user)

    if data == "home":
        context.user_data.pop("buying", None)
        await q.edit_message_text(HOME_TEXT, reply_markup=home_markup(), parse_mode=ParseMode.HTML)
    elif data == "my":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home")]])
        await q.edit_message_text(await my_subs_text(q.from_user.id), reply_markup=kb, parse_mode=ParseMode.HTML)
