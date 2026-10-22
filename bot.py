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
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ExtBot,
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
def norm_url(u, default=""):
    """Telegram button ke liye URL theek karo: '@name' / 't.me/name' / 'name.com' -> https://..."""
    u = (u or "").strip().strip("\"'").strip()
    if not u:
        return default
    if u.startswith("@"):
        return "https://t.me/" + u[1:]
    if u.startswith(("http://", "https://", "tg://")):
        return u
    return "https://" + u


CONTACT_URL = norm_url(os.getenv("CONTACT_URL"), "https://t.me/dghelps_bot")  # Admin Support button

IST = ZoneInfo("Asia/Kolkata")
HOUR, DAY = 3600, 86400
REMIND_DAYS = 2  # expiry se itne din pehle reminder

# ---- /availableotts ka reply. Yahan badal do ----
AVAILABLE_OTTS_TEXT = '''<pre>📌AVAILABLE OTT's &amp; Services</pre>
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
"Pocket FM" 🀄️
"Prime Video" 🔵
"STAGE" 🌀
"SHEMAROO ME" 📽
"SONY LIV" 🟣
"Sun NXT" 🎃
"ZEE 5" ♈️
<blockquote>📌 Total = 17 OTT's<blockquote>


<blockquote>👾ALL Daily Shows 👾</blockquote>
"TV SHOWS"🌈
"Colors"
"Dangal-Play"
"Jio-Hotstar"
"SonyLivv" ☘️
"Sun NXT"
"ZEE-5"

<blockquote><i>Example: ↓↓</i></blockquote>
"Taarak Mehta"
"Bigg Boss"
"New-CiD S03"
"Pushpa Impossible"
"Kumkum Bhagya"
"All Other On-going Shows"

<blockquote>Weekly Releases Show's ⚡️</blockquote>
"Reality Show's"
"Laughter Chef"
"India's Got Talent"
"All Other Shows"
'''

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


def norm_chat_id(v):
    v = (v or "").strip().strip("\"'").strip()
    if v.isdigit() and v.startswith("100") and len(v) >= 12:
        v = "-" + v  # -100 wala minus bhoolne par auto-fix
    try:
        return int(v)
    except ValueError:
        return None


def get_plan(pid):
    return next((p for p in PLANS if p["id"] == pid), None)


def pname(key):
    return PRODUCTS.get(key, {}).get("name", key)


def channel_of(key):
    return norm_chat_id(os.getenv(PRODUCTS[key]["channel_env"], ""))


def all_channels():
    return sorted({channel_of(k) for k in PRODUCTS if channel_of(k)})


# ================================================================ AUTO CLEAN
AUTO_DELETE_SECS = 300
_bg_tasks = set()


class PremiumBot(ExtBot):
    """User ke private chat mein bheje gaye messages kuch der baad khud saaf ho jaate hain.
    Admin chats aur auto_delete=False wale messages bache rehte hain."""

    async def _delete_later(self, chat_id, message_id):
        await asyncio.sleep(AUTO_DELETE_SECS)
        try:
            await self.delete_message(chat_id, message_id)
        except Exception:
            pass

    def _schedule(self, msg, auto_delete):
        try:
            cid = msg.chat_id
            if not auto_delete or cid <= 0 or cid in ADMIN_IDS:
                return
            t = asyncio.get_running_loop().create_task(self._delete_later(cid, msg.message_id))
            _bg_tasks.add(t)
            t.add_done_callback(_bg_tasks.discard)
        except Exception:
            pass

    async def send_message(self, *args, auto_delete=True, **kwargs):
        msg = await super().send_message(*args, **kwargs)
        self._schedule(msg, auto_delete)
        return msg

    async def send_photo(self, *args, auto_delete=True, **kwargs):
        msg = await super().send_photo(*args, **kwargs)
        self._schedule(msg, auto_delete)
        return msg


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
def home_text(name, thanks=False):
    name = esc(name or "there")
    head = (
        f"🎉 <b>Thank you for joining, {name}!</b>\n✅ Verification complete\n\n"
        if thanks
        else f"✨ <b>Welcome, {name}!</b> ✨\n\n"
    )
    return (
        head
        + "<blockquote>💎 <b>Premium Access Hub</b>\n"
        "Apna plan chunein aur access paayein — simple, fast aur secure.</blockquote>\n\n"
        "⚡ Payment verify hote hi activation\n"
        "🔒 Secure UPI payment\n"
        "📺 /availableotts — kya-kya available hai dekhein\n\n"
        "👇 <b>Neeche se apna plan choose karein</b>"
    )


JOIN_TEXT = (
    "🔐 <b>Members-Only Access</b>\n\n"
    "<blockquote>Premium services sirf hamare official channel ke members ke liye available hain.</blockquote>\n\n"
    "📢 Neeche <b>Join Official Channel</b> dabayein\n"
    "✨ Join karte hi aapka access activate ho jayega"
)


def bye_text(name):
    return (
        f"👋 <b>Goodbye, {esc(name or 'there')}</b>\n\n"
        "Channel leave hote hi aapka access pause ho gaya hai 🔒\n"
        "Jab chahein wapas join karke /start dabayein — hum yahin hain 💎"
    )


# ---------------- Force join ----------------
def force_channel():
    return norm_chat_id(os.getenv("FORCE_CHANNEL", ""))


_force_link = {"v": norm_url(os.getenv("FORCE_LINK"))}
JOIN_PROMPTS = {}   # user_id -> [message_id]
LAST_MENU = {}      # user_id -> welcome message_id


async def purge_join_prompts(bot, uid):
    for mid in JOIN_PROMPTS.pop(uid, []):
        try:
            await bot.delete_message(uid, mid)
        except Exception:
            pass


async def drop_last_menu(bot, uid):
    mid = LAST_MENU.pop(uid, None)
    if mid:
        try:
            await bot.delete_message(uid, mid)
        except Exception:
            pass


async def get_force_link(context):
    if _force_link["v"]:
        return _force_link["v"]
    ch = force_channel()
    if not ch:
        return None
    try:
        _force_link["v"] = await context.bot.export_chat_invite_link(ch)
        return _force_link["v"]
    except Exception as e:
        log.warning("Force channel ka link nahi bana (FORCE_LINK set karo): %s", e)
        return None


def _in_status(status, is_member=None):
    return status in ("creator", "administrator", "member") or (status == "restricted" and bool(is_member))


async def is_member(context, ch, uid):
    try:
        m = await context.bot.get_chat_member(ch, uid)
        return _in_status(m.status, getattr(m, "is_member", None))
    except Exception as e:
        # bot channel mein admin nahi / galat ID: user ko rokna nahi, bas log karo
        log.warning("Force-join check fail (bot channel mein admin hai?): %s", e)
        return True


async def ensure_joined(update, context):
    """True = aage badho. False = join prompt bhej diya."""
    ch = force_channel()
    u = update.effective_user
    if not ch or is_admin(u.id):
        return True
    if await is_member(context, ch, u.id):
        if u.id in JOIN_PROMPTS:
            await purge_join_prompts(context.bot, u.id)
        return True
    await purge_join_prompts(context.bot, u.id)
    link = await get_force_link(context)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📢 Join Official Channel", url=link)]]) if link else None
    q = update.callback_query
    if q:
        try:
            await q.answer("🔐 Pehle channel join karein", show_alert=True)
        except Exception:
            pass
    m = await context.bot.send_message(u.id, JOIN_TEXT, reply_markup=kb, parse_mode=ParseMode.HTML)
    JOIN_PROMPTS.setdefault(u.id, []).append(m.message_id)
    return False


async def forcecheck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: force-join sahi set hai ya nahi, check karo."""
    if not is_admin(update.effective_user.id):
        return
    raw = os.getenv("FORCE_CHANNEL")
    ch = force_channel()
    if not ch:
        await update.message.reply_text(
            f"❌ FORCE_CHANNEL set nahi hai ya galat hai (abhi value: {raw!r}).\n"
            "Render → Environment mein -100 wali channel ID daalo, phir redeploy karo."
        )
        return
    lines = [f"FORCE_CHANNEL: {ch}"]
    try:
        chat = await context.bot.get_chat(ch)
        lines.append(f"Channel: {chat.title}")
        me = await context.bot.get_chat_member(ch, context.bot.id)
        lines.append(f"Bot status: {me.status}")
        if me.status == "administrator":
            lines.append("✅ Bot admin hai, force-join chalna chahiye.")
        else:
            lines.append("⚠️ Bot ko is channel mein ADMIN banao, warna force-join kaam nahi karega.")
    except Exception as e:
        lines.append(f"❌ Error: {e}\n(ID galat hai ya bot channel mein hai hi nahi)")
    lines.append(f"Join link: {await get_force_link(context) or 'nahi bana (FORCE_LINK daalo)'}")
    lines.append("\nNote: admin accounts ko join prompt nahi dikhta. Test dusre account se karo.")
    await update.message.reply_text("\n".join(lines))


async def on_force_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force channel mein join/leave hote hi bot khud message bhejta hai."""
    cm = update.chat_member
    ch = force_channel()
    if not cm or not ch or cm.chat.id != ch:
        return
    user = cm.new_chat_member.user
    if user.is_bot:
        return
    was_in = _in_status(cm.old_chat_member.status, getattr(cm.old_chat_member, "is_member", None))
    now_in = _in_status(cm.new_chat_member.status, getattr(cm.new_chat_member, "is_member", None))
    # bot sirf unhe message kar sakta hai jinhone bot ko kabhi start kiya ho
    if await db.users.find_one({"_id": user.id}) is None or await is_banned(user.id):
        return
    try:
        if now_in and not was_in:
            await purge_join_prompts(context.bot, user.id)
            await drop_last_menu(context.bot, user.id)
            m = await context.bot.send_message(
                user.id, start_text(user.first_name or user.full_name, thanks=True),
                reply_markup=start_markup(), parse_mode=ParseMode.HTML,
            )
            LAST_MENU[user.id] = m.message_id
        elif was_in and cm.new_chat_member.status == "left":
            await context.bot.send_message(
                user.id, bye_text(user.first_name or user.full_name), parse_mode=ParseMode.HTML
            )
    except Exception as e:
        log.info("Force-channel message nahi gaya %s: %s", user.id, e)


def home_markup():
    """'Available Services' screen: products + OTT list + my subscriptions."""
    rows = [[InlineKeyboardButton(p["name"], callback_data=f"p:{k}")] for k, p in PRODUCTS.items()]
    rows.append([InlineKeyboardButton("📺 Available OTTs", callback_data="otts")])
    rows.append([InlineKeyboardButton("📋 My Subscriptions", callback_data="my")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="start")])
    return InlineKeyboardMarkup(rows)


def start_text(name, thanks=False):
    """/start par dikhne wala chhota welcome."""
    name = esc(name or "there")
    head = (
        f"🎉 <b>Thank you for joining, {name}!</b>\n✅ Verification complete"
        if thanks
        else f"✨ <b>Welcome, {name}!</b>"
    )
    return (
        head
        + "\n\n"
        + f"💎 WELCOME, {name}! Kese Ho Aap 😁\n"
        + "\n\n"
        + " <b>WHAT WE PROViDES ?<b>\n"
        + "<b>• A Seamless Entertainment🕺Hub 📡\n"
        + "• 15 Million+💫 Content Served Over (6 Years)\n"
        + "• For Those Who Belive in Experience⚡& Quality..\n"
        + "• All Populor OTTs\n\n"
        + '🛡️ Developed By: DMT BOTS (https://t.me/DMTBOTS)<b>'
        + "\n\n👇 Select Option To Continue "
    )


def start_markup(support=True):
    rows = [
        [InlineKeyboardButton("📂 Available Services", callback_data="src")],
        [InlineKeyboardButton("🌐 Terms & Conditions", callback_data="terms")],
    ]
    if support:
        rows.append([InlineKeyboardButton("🛟 Admin Support", url=CONTACT_URL)])
    return InlineKeyboardMarkup(rows)


# Terms & Conditions: abhi blank. Apna text yahan "..." ke andar likho.
TERMS_TEXT = (
    "🌐 <b>Terms &amp; Conditions/n"
"Read Carefully/n"
"<blockquote>• Here Of Your Purchase Top-Up/ Primium Plan You'll  Easily Access Ur Content "24×7" Till You Have Subscription./n"
"• There Is a Limited Stock Of Entertainment Package In Which :- HDTC - Fiilms / OTT-Realeses /Web-Seriies / TV-Shhows / TV Seriials are Included./n"
"• Dear User If You Have Any Other Queries/Problem U-Can Directly Contact/n"
"@DGHelps_Bot (Our 24×7 Support Chat)/n"
"Thanks regards/n" 
"© copyright holder I'd 8763xxxx/n"
"® Registered Tezstudus.In co<blockquote>"
﻿</b>\n\n"
    ""
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await touch_user(u)
    if await is_banned(u.id):
        await update.effective_message.reply_text("🚫 Aap is bot ko use nahi kar sakte.")
        return
    if not await ensure_joined(update, context):
        return
    context.user_data.pop("buying", None)
    await drop_last_menu(context.bot, u.id)
    text = start_text(u.first_name or u.full_name)
    try:
        m = await update.effective_message.reply_text(
            text, reply_markup=start_markup(), parse_mode=ParseMode.HTML
        )
    except Exception as e:
        log.error("Start screen (support button ke saath) fail: %s | CONTACT_URL=%r", e, CONTACT_URL)
        m = await update.effective_message.reply_text(
            text, reply_markup=start_markup(support=False), parse_mode=ParseMode.HTML
        )
    LAST_MENU[u.id] = m.message_id
    try:
        await update.effective_message.delete()
    except Exception:
        pass


async def availableotts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await touch_user(u)
    if await is_banned(u.id):
        return
    if not await ensure_joined(update, context):
        return
    await update.message.reply_text(AVAILABLE_OTTS_TEXT, parse_mode=ParseMode.HTML)


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
    if not await ensure_joined(update, context):
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

    if data in ("home", "src"):
        context.user_data.pop("buying", None)
        await q.edit_message_text(
            home_text(q.from_user.first_name or q.from_user.full_name),
            reply_markup=home_markup(), parse_mode=ParseMode.HTML,
        )
    elif data == "start":
        context.user_data.pop("buying", None)
        await q.edit_message_text(
            start_text(q.from_user.first_name or q.from_user.full_name),
            reply_markup=start_markup(), parse_mode=ParseMode.HTML,
        )
    elif data == "terms":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="start")]])
        await q.edit_message_text(TERMS_TEXT, reply_markup=kb, parse_mode=ParseMode.HTML)
    elif data == "otts":
        await context.bot.send_message(q.from_user.id, AVAILABLE_OTTS_TEXT, parse_mode=ParseMode.HTML)
    elif data == "my":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home")]])
        await q.edit_message_text(await my_subs_text(q.from_user.id), reply_markup=kb, parse_mode=ParseMode.HTML)
    elif data.startswith("p:"):
        key = data[2:]
        if key in PRODUCTS:
            context.user_data.pop("buying", None)
            await show_product(q, key)
    elif data.startswith("plan:"):
        _, key, pid = data.split(":")
        if key in PRODUCTS:
            await show_payment(q, context, key, pid)


async def mysubs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await touch_user(update.effective_user)
    if await is_banned(update.effective_user.id) or not await ensure_joined(update, context):
        return
    await update.message.reply_text(await my_subs_text(update.effective_user.id), parse_mode=ParseMode.HTML)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if await is_banned(u.id):
        return
    await touch_user(u)
    if not await ensure_joined(update, context):
        return
    buying = context.user_data.get("buying")
    pl = get_plan(buying["plan"]) if buying else None
    if not buying or not pl:
        await update.message.reply_text("Pehle /start karke plan choose karein.")
        return

    key = buying["key"]
    file_id = update.message.photo[-1].file_id
    req_id = await next_req_id()
    await db.requests.insert_one({
        "_id": req_id, "user_id": u.id, "username": u.username, "full_name": u.full_name,
        "product": key, "status": "pending", "file_id": file_id, "created_at": int(time.time()),
        "plan_secs": pl["secs"], "plan_label": pl["label"], "plan_price": pl["price"],
    })
    context.user_data.pop("buying", None)
    await update.message.reply_text("✅ Screenshot mil gaya! Admin verify karke jaldi approve karenge.")

    caption = (
        f"🧾 <b>New Payment Request #{req_id}</b>\n\n"
        f"👤 {esc(u.full_name)} (@{esc(u.username) or '-'})\n"
        f"🆔 <code>{u.id}</code>\n"
        f"📦 {esc(PRODUCTS[key]['name'])}\n"
        f"⏳ Plan: {pl['label']} — ₹{pl['price']}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Approve ({fmt_dur(pl['secs'])})", callback_data=f"ap:{req_id}")],
        [InlineKeyboardButton("⏱ Change duration", callback_data=f"chg:{req_id}"),
         InlineKeyboardButton("❌ Reject", callback_data=f"rj:{req_id}")],
    ])
    for admin in ADMIN_IDS:
        try:
            await context.bot.send_photo(admin, file_id, caption=caption, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning("Admin %s ko send nahi hua: %s", admin, e)


# ================================================================ ADMIN APPROVAL
def duration_keyboard(req_id):
    rows, row = [], []
    for secs, label in DURATION_CHOICES:
        row.append(InlineKeyboardButton(label, callback_data=f"dur:{req_id}:{secs}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✏️ Custom days", callback_data=f"cd:{req_id}")])
    rows.append([InlineKeyboardButton("❌ Reject", callback_data=f"rj:{req_id}")])
    return InlineKeyboardMarkup(rows)


async def finalize(context, req_id, secs):
    """Request approve karo, subscription banao/badhao, user ko link bhejo. Summary text return."""
    now = int(time.time())
    # atomic: sirf ek hi baar approve ho (double click safe)
    res = await db.requests.update_one(
        {"_id": req_id, "status": "pending"}, {"$set": {"status": "approved"}}
    )
    if res.modified_count == 0:
        return "Request pehle hi handle ho chuki hai."

    req = await db.requests.find_one({"_id": req_id})
    key, uid = req["product"], req["user_id"]
    sub_id = f"{uid}:{key}"
    sub = await db.subs.find_one({"_id": sub_id})
    if sub and sub.get("active") and sub["expiry_ts"] > now:
        start, base = sub["start_ts"], sub["expiry_ts"]  # renew: purani expiry se aage badhao
    else:
        start, base = now, now
    expiry = base + secs
    reminded = expiry - now <= REMIND_DAYS * DAY

    await db.subs.update_one(
        {"_id": sub_id},
        {"$set": {"user_id": uid, "product": key, "start_ts": start,
                  "expiry_ts": expiry, "active": True, "reminded": reminded}},
        upsert=True,
    )

    text = (
        f"✅ <b>Payment approved!</b>\n\n📦 {esc(pname(key))}\n"
        f"🗓 Joining: {fmt(start)}\n⏳ Expiry: <b>{fmt(expiry)}</b>\n\n"
    )
    note = ""
    try:
        ttl = min(DAY, expiry - now)
        link = await make_invite(context, key, ttl)
        text += f"🔗 Join link (sirf 1 banda, {link_ttl_text(ttl)} valid):\n{link}"
    except Exception as e:
        log.error("Invite link fail: %s", e)
        note = f"\n⚠️ Invite link nahi bana: {e}\nManually bhejo: /resend {uid} {key}"
        text += "Admin aapko jaldi channel link bhej denge."
    try:
        await context.bot.send_message(uid, text, parse_mode=ParseMode.HTML, auto_delete=False)
    except Exception as e:
        note += f"\n⚠️ User ko message nahi gaya: {e}"

    return f"✅ <b>Approved</b> — {fmt_dur(secs)}, expiry {fmt(expiry)}{esc(note)}"


async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Sirf admin ke liye", show_alert=True)
        return
    await q.answer()
    parts = q.data.split(":")
    action, req_id = parts[0], int(parts[1])
    req = await db.requests.find_one({"_id": req_id})

    if not req or req["status"] != "pending":
        try:
            await q.edit_message_reply_markup(None)
        except Exception:
            pass
        await q.message.reply_text(f"Request #{req_id} pehle hi handle ho chuki hai.")
        return

    if action == "ap":
        summary = await finalize(context, req_id, int(req["plan_secs"]))
        await q.edit_message_caption((q.message.caption_html or "") + "\n\n" + summary, parse_mode=ParseMode.HTML)
    elif action == "chg":
        await q.edit_message_reply_markup(duration_keyboard(req_id))
    elif action == "cd":
        context.user_data["await_days"] = req_id
        await q.message.reply_text(f"Request #{req_id}: din ki sankhya number mein bhejein (e.g. 45)")
    elif action == "dur":
        summary = await finalize(context, req_id, int(parts[2]))
        await q.edit_message_caption((q.message.caption_html or "") + "\n\n" + summary, parse_mode=ParseMode.HTML)
    elif action == "rj":
        await db.requests.update_one({"_id": req_id, "status": "pending"}, {"$set": {"status": "rejected"}})
        await q.edit_message_caption((q.message.caption_html or "") + "\n\n❌ <b>Rejected</b>", parse_mode=ParseMode.HTML)
        try:
            await context.bot.send_message(
                req["user_id"],
                "❌ Aapka payment verify nahi ho paya. Sahi screenshot ke saath dobara try karein ya admin se contact karein.",
                auto_delete=False,
            )
        except Exception:
            pass


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    # admin ka custom-days input
    if is_admin(uid) and context.user_data.get("await_days"):
        t = update.message.text.strip()
        if not t.isdigit() or not (0 < int(t) <= 3650):
            await update.message.reply_text("Sahi number bhejein (1-3650).")
            return
        req_id = context.user_data.pop("await_days")
        summary = await finalize(context, req_id, int(t) * DAY)
        await update.message.reply_text(summary, parse_mode=ParseMode.HTML)
        return
    if not await is_banned(uid):
        await touch_user(update.effective_user)
        if not await ensure_joined(update, context):
            return
        await update.message.reply_text("/start dabake menu kholein.")


# ================================================================ ADMIN COMMANDS
async def subs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    now = int(time.time())
    rows = await db.subs.find({"active": True, "expiry_ts": {"$gt": now}}).to_list(None)
    rows.sort(key=lambda r: r["expiry_ts"])
    if not rows:
        await update.message.reply_text("Koi active subscriber nahi.")
        return
    users = {u["_id"]: u for u in await db.users.find({}).to_list(None)}
    lines = [f"<b>Active subscribers ({len(rows)})</b>\n"]
    for r in rows:
        u = users.get(r["user_id"], {})
        lines.append(
            f"• {esc(u.get('full_name'))} (@{esc(u.get('username')) or '-'}) <code>{r['user_id']}</code>\n"
            f"  {esc(pname(r['product']))} → {fmt(r['expiry_ts'])}"
        )
    text = "\n".join(lines)
    for i in range(0, len(text), 3800):
        await update.message.reply_text(text[i:i + 3800], parse_mode=ParseMode.HTML)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    now = int(time.time())
    users = await db.users.count_documents({})
    pending = await db.requests.count_documents({"status": "pending"})
    banned = await db.banned.count_documents({})
    approved = await db.requests.find({"status": "approved"}).to_list(None)
    paid_sum = sum(r.get("plan_price") or 0 for r in approved)
    active = await db.subs.find({"active": True, "expiry_ts": {"$gt": now}}).to_list(None)
    per = {}
    for s in active:
        per[s["product"]] = per.get(s["product"], 0) + 1
    lines = [
        f"👥 Total users: {users}", f"🚫 Banned: {banned}", f"⏳ Pending requests: {pending}",
        f"💰 Approved payments: {len(approved)} (₹{paid_sum})", "", "Active subs:",
    ]
    lines += [f"• {pname(k)}: {n}" for k, n in per.items()] or ["• none"]
    await update.message.reply_text("\n".join(lines))


async def revoke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/revoke <user_id> <product>"""
    if not is_admin(update.effective_user.id):
        return
    a = context.args
    if len(a) != 2 or a[1] not in PRODUCTS or not a[0].isdigit():
        await update.message.reply_text("Use: /revoke <user_id> <product>\nProducts: " + ", ".join(PRODUCTS))
        return
    uid, key = int(a[0]), a[1]
    await kick(context, uid, key)
    await db.subs.update_one({"_id": f"{uid}:{key}"}, {"$set": {"active": False}})
    await update.message.reply_text("Done ✅")


async def resend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/resend <user_id> <product> -> active subscriber ko naya join link"""
    if not is_admin(update.effective_user.id):
        return
    a = context.args
    if len(a) != 2 or a[1] not in PRODUCTS or not a[0].isdigit():
        await update.message.reply_text("Use: /resend <user_id> <product>\nProducts: " + ", ".join(PRODUCTS))
        return
    uid, key = int(a[0]), a[1]
    now = int(time.time())
    sub = await db.subs.find_one({"_id": f"{uid}:{key}", "active": True, "expiry_ts": {"$gt": now}})
    if not sub:
        await update.message.reply_text("Is user ka is plan mein active subscription nahi hai.")
        return
    try:
        ttl = min(DAY, sub["expiry_ts"] - now)
        link = await make_invite(context, key, ttl)
        await context.bot.send_message(
            uid, f"🔗 {pname(key)} join link (sirf 1 banda, {link_ttl_text(ttl)} valid):\n{link}",
            auto_delete=False,
        )
        await update.message.reply_text("Link bhej diya ✅")
    except Exception as e:
        await update.message.reply_text(f"❌ Fail: {e}")


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ban <user_id> -> bot use band + subscriptions band + sab channels se ban"""
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Use: /ban <user_id>")
        return
    uid = int(context.args[0])
    if is_admin(uid):
        await update.message.reply_text("Admin ko ban nahi kar sakte.")
        return
    await db.banned.update_one({"_id": uid}, {"$setOnInsert": {"at": int(time.time())}}, upsert=True)
    await db.subs.update_many({"user_id": uid}, {"$set": {"active": False}})
    lines = [f"🚫 {uid} banned."]
    for ch in all_channels():
        try:
            await context.bot.ban_chat_member(ch, uid)
            lines.append(f"✅ channel {ch}: banned")
        except Exception as e:
            lines.append(f"❌ channel {ch}: {e}")
    await update.message.reply_text("\n".join(lines))


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Use: /unban <user_id>")
        return
    uid = int(context.args[0])
    await db.banned.delete_one({"_id": uid})
    lines = [f"✅ {uid} unbanned."]
    for ch in all_channels():
        try:
            await context.bot.unban_chat_member(ch, uid, only_if_banned=True)
            lines.append(f"✅ channel {ch}: unbanned")
        except Exception as e:
            lines.append(f"❌ channel {ch}: {e}")
    await update.message.reply_text("\n".join(lines))


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/broadcast <text>  ya kisi message ko reply karke /broadcast"""
    if not is_admin(update.effective_user.id):
        return
    msg = update.message
    reply = msg.reply_to_message
    parts = msg.text.split(maxsplit=1)
    text = parts[1] if len(parts) > 1 else ""
    if not reply and not text:
        await msg.reply_text("Use:\n/broadcast <message>\nYa kisi message ko reply karke /broadcast likho.")
        return
    banned = {b["_id"] for b in await db.banned.find({}).to_list(None)}
    ids = [u["_id"] for u in await db.users.find({}).to_list(None) if u["_id"] not in banned]
    await msg.reply_text(f"📢 Broadcast shuru: {len(ids)} users...")
    admin_id = update.effective_user.id

    async def run():
        sent = failed = 0
        for uid in ids:
            for _ in range(2):
                try:
                    if reply:
                        await context.bot.copy_message(uid, reply.chat_id, reply.message_id)
                    else:
                        await context.bot.send_message(uid, text, auto_delete=False)
                    sent += 1
                    break
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except Exception:
                    failed += 1
                    break
            await asyncio.sleep(0.05)
        await context.bot.send_message(admin_id, f"📢 Broadcast done ✅\nSent: {sent}\nFailed: {failed}")

    context.application.create_task(run())


async def setqr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """QR ki photo par reply karke /setqr"""
    if not is_admin(update.effective_user.id):
        return
    r = update.message.reply_to_message
    if not r or not r.photo:
        await update.message.reply_text("QR ki photo bhejo, phir us photo par reply karke /setqr likho.")
        return
    await set_setting("qr_file_id", r.photo[-1].file_id)
    await update.message.reply_text("✅ QR set ho gaya.")


async def removeqr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await db.settings.delete_one({"_id": "qr_file_id"})
    await update.message.reply_text("QR hata diya.")


async def forwarded_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin kisi channel ka message bot ko forward kare -> channel ki ID mil jayegi"""
    if not is_admin(update.effective_user.id):
        return
    origin = getattr(update.message, "forward_origin", None)
    chat = getattr(origin, "chat", None)
    if not chat:
        await update.message.reply_text("Ye message kisi channel se forward nahi hua.")
        return
    await update.message.reply_text(f"📡 {chat.title}\nID: {chat.id}")


# ================================================================ EXPIRY JOB
async def expiry_job(context: ContextTypes.DEFAULT_TYPE):
    now = int(time.time())
    expired = await db.subs.find({"active": True, "expiry_ts": {"$lte": now}}).to_list(None)
    remind = await db.subs.find({
        "active": True, "reminded": False,
        "expiry_ts": {"$gt": now, "$lte": now + REMIND_DAYS * DAY},
    }).to_list(None)

    for r in remind:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Renew", callback_data=f"p:{r['product']}")]])
        try:
            await context.bot.send_message(
                r["user_id"],
                f"⚠️ Aapka <b>{esc(pname(r['product']))}</b> subscription "
                f"<b>{fmt(r['expiry_ts'])}</b> ko expire hoga. Renew kar lein!",
                reply_markup=kb, parse_mode=ParseMode.HTML, auto_delete=False,
            )
        except Exception:
            pass
        await db.subs.update_one({"_id": r["_id"]}, {"$set": {"reminded": True}})

    for r in expired:
        key, uid = r["product"], r["user_id"]
        # sirf tab expire karo jab beech mein renew na hua ho
        res = await db.subs.update_one(
            {"_id": r["_id"], "expiry_ts": r["expiry_ts"], "active": True}, {"$set": {"active": False}}
        )
        if res.modified_count == 0:
            continue
        await kick(context, uid, key)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Re-new Subscription", callback_data=f"p:{key}")]])
        try:
            await context.bot.send_message(
                uid,
                f"⏰ Aapka <b>{esc(pname(key))}</b> subscription expire ho gaya hai.\n"
                "Dobara access ke liye please <b>re-new your subscription</b> 👇",
                reply_markup=kb, parse_mode=ParseMode.HTML, auto_delete=False,
            )
        except Exception:
            pass
        for admin in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin, f"⏰ Expired & removed: <code>{uid}</code> — {esc(pname(key))}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass


# ================================================================ MAIN
def start_health_server():
    """Render jaise hosts 'Web Service' mein port maangte hain. PORT set ho tabhi chalta hai."""
    port = os.getenv("PORT")
    if not port:
        return
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("0.0.0.0", int(port)), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health server port %s pe chalu", port)


def main():
    start_health_server()
    app = (
        Application.builder().bot(PremiumBot(BOT_TOKEN))
        .post_init(post_init).post_shutdown(post_shutdown).build()
    )

    # user commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mysubs", mysubs_cmd))
    app.add_handler(CommandHandler("availableotts", availableotts_cmd))  # <-- naya command

    # admin commands
    app.add_handler(CommandHandler("subs", subs_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("revoke", revoke_cmd))
    app.add_handler(CommandHandler("resend", resend_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("setqr", setqr_cmd))
    app.add_handler(CommandHandler("removeqr", removeqr_cmd))
    app.add_handler(CommandHandler("forcecheck", forcecheck_cmd))

    # buttons
    app.add_handler(CallbackQueryHandler(admin_cb, pattern=r"^(ap|chg|cd|dur|rj):"))
    app.add_handler(CallbackQueryHandler(user_cb, pattern=r"^(home|src|start|terms|my|otts|qr|p:.*|plan:.*)$"))

    # force-channel join/leave tracking
    app.add_handler(ChatMemberHandler(on_force_member, ChatMemberHandler.CHAT_MEMBER))

    # messages
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, on_photo))
    app.add_handler(MessageHandler(filters.FORWARDED & filters.ChatType.PRIVATE, forwarded_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))

    # har 60 sec mein expiry check
    app.job_queue.run_repeating(expiry_job, interval=60, first=15)

    log.info("Bot chalu ho gaya")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
