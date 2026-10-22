"""Premium Telegram subscription bot.
Features: plans, UPI/QR payment screenshots, admin approval, one-use invite links,
MongoDB subscriptions, expiry/reminders, force-join, admin tools and /availableotts.
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
    Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler,
    ContextTypes, ExtBot, MessageHandler, filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("subbot")

# ----------------------------- CONFIG -----------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
MONGO_DB = os.getenv("MONGO_DB", "subbot")
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(",", " ").split()
    if x.isdigit()
}
UPI_ID = os.getenv("UPI_ID", "yourname@upi")
IST = ZoneInfo("Asia/Kolkata")
HOUR, DAY = 3600, 86400
REMIND_DAYS = 2
AUTO_DELETE_SECS = 300
_bg_tasks = set()

def norm_url(value, default=""):
    value = (value or "").strip().strip("\"'").strip()
    if not value:
        return default
    if value.startswith("@"):
        return "https://t.me/" + value[1:]
    if value.startswith(("https://", "http://", "tg://")):
        return value
    return "https://" + value

CONTACT_URL = norm_url(os.getenv("CONTACT_URL"), "https://t.me/dghelps_bot")
_force_link = {"v": norm_url(os.getenv("FORCE_LINK"))}

def norm_chat_id(value):
    value = (value or "").strip().strip("\"'").strip()
    if value.isdigit() and value.startswith("100") and len(value) >= 12:
        value = "-" + value
    try:
        return int(value)
    except (ValueError, TypeError):
        return None

PRODUCTS = {
    "movies": {
        "name": "🎬 Movies Premium",
        "terms": "• Payment non-refundable as per stated policy\n• Personal use only\n• Sharing invite links is prohibited",
        "channel_env": "CH_MOVIES",
    },
    "music": {
        "name": "🎧 Music Premium",
        "terms": "• Payment non-refundable as per stated policy\n• Personal use only\n• Sharing invite links is prohibited",
        "channel_env": "CH_MUSIC",
    },
}
PLANS = [
    {"id": "60d", "label": "🥇 60 Days", "secs": 60 * DAY, "price": 79},
    {"id": "30d", "label": "🥈 30 Days", "secs": 30 * DAY, "price": 39},
    {"id": "7d", "label": "🥉 7 Days", "secs": 7 * DAY, "price": 19},
    {"id": "3h", "label": "⚜️ 3 Hours", "secs": 3 * HOUR, "price": 9},
]
DURATION_CHOICES = [
    (3 * HOUR, "3h"), (7 * DAY, "7d"), (15 * DAY, "15d"),
    (30 * DAY, "30d"), (60 * DAY, "60d"), (90 * DAY, "90d"),
    (180 * DAY, "180d"), (365 * DAY, "365d"),
]

AVAILABLE_OTTS_TEXT = """<pre>📌 AVAILABLE OTTs &amp; Services</pre>
"Alt Balaji" 🅰️
"Aha" 🔶
"Arey Digital" 🎬
"Amazon Mini TV" 📺
"Bongo TV" 🌐
"Dangal Play" 🟣
"Discovery+" 🎱
"JioHotstar" 💠
"JioCinema" 🌸
"Netflix" 🧧
"Pocket FM" 🀄️
"Prime Video" 🔵
"STAGE" 🌀
"ShemarooMe" 📽
"SonyLIV" 🟣
"Sun NXT" 🎃
"ZEE5" ♈️

<blockquote>📌 Total = 17 OTTs</blockquote>

<blockquote>👾 ALL Daily Shows 👾</blockquote>
TV SHOWS 🌈
Colors
Dangal Play
JioHotstar
SonyLIV ☘️
Sun NXT
ZEE5

<blockquote><i>Examples:</i>
Taarak Mehta
Bigg Boss
New-CID S03
Pushpa Impossible
Kumkum Bhagya
All other ongoing shows</blockquote>

<blockquote>⚡ Weekly Releases
Reality Shows
Laughter Chef
India's Got Talent
All other shows</blockquote>"""

TERMS_TEXT = (
    "🌐 <b>Terms &amp; Conditions</b>\n\n"
    "<blockquote>"
    "• Access remains available until the subscription expiry shown by the bot.\n\n"
    "• Content availability and quality may vary by source.\n\n"
    "• Do not share account details or invite links.\n\n"
    "• Access is activated after payment verification.\n\n"
    "• Please confirm the refund policy before paying.\n\n"
    "• Contact support for questions or issues."
    "</blockquote>\n\n"
    "🛟 <b>Support:</b> @DGHelps_Bot\n"
    "© All rights reserved."
)

# ----------------------------- HELPERS -----------------------------
def esc(value):
    return html.escape(str(value) if value is not None else "")

def is_admin(uid):
    return uid in ADMIN_IDS

def get_plan(pid):
    return next((p for p in PLANS if p["id"] == pid), None)

def pname(key):
    return PRODUCTS.get(key, {}).get("name", key)

def channel_of(key):
    return norm_chat_id(os.getenv(PRODUCTS[key]["channel_env"], ""))

def all_channels():
    return sorted({channel_of(k) for k in PRODUCTS if channel_of(k)})

def fmt(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%d %b %Y, %I:%M %p")

def fmt_dur(secs):
    if secs < DAY:
        return f"{max(1, secs // HOUR)} Hours"
    return f"{secs // DAY} Days"

def fmt_left(secs):
    if secs >= DAY:
        return f"{secs // DAY} days"
    if secs >= HOUR:
        return f"{secs // HOUR} hours"
    return f"{max(1, secs // 60)} minutes"

async def is_banned(uid):
    if is_admin(uid):
        return False
    return await db.banned.find_one({"_id": uid}) is not None

async def touch_user(user):
    now = int(time.time())
    await db.users.update_one(
        {"_id": user.id},
        {"$set": {"username": user.username, "full_name": user.full_name, "last_seen": now},
         "$setOnInsert": {"first_seen": now}},
        upsert=True,
    )

# ----------------------------- AUTO-DELETE BOT -----------------------------
class PremiumBot(ExtBot):
    async def _delete_later(self, chat_id, message_id):
        await asyncio.sleep(AUTO_DELETE_SECS)
        try:
            await self.delete_message(chat_id, message_id)
        except Exception:
            pass

    def _schedule(self, msg, auto_delete):
        try:
            if not auto_delete or msg.chat_id <= 0 or msg.chat_id in ADMIN_IDS:
                return
            task = asyncio.get_running_loop().create_task(
                self._delete_later(msg.chat_id, msg.message_id)
            )
            _bg_tasks.add(task)
            task.add_done_callback(_bg_tasks.discard)
        except Exception:
            log.exception("Could not schedule auto-delete")

    async def send_message(self, *args, auto_delete=True, **kwargs):
        msg = await super().send_message(*args, **kwargs)
        self._schedule(msg, auto_delete)
        return msg

    async def send_photo(self, *args, auto_delete=True, **kwargs):
        msg = await super().send_photo(*args, **kwargs)
        self._schedule(msg, auto_delete)
        return msg

# ----------------------------- DATABASE -----------------------------
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
    result = await db.settings.find_one({"_id": key})
    return result.get("value") if result else None

async def set_setting(key, value):
    await db.settings.update_one(
        {"_id": key}, {"$set": {"value": value}}, upsert=True
    )

# ----------------------------- INVITES / MEMBERSHIP -----------------------------
def force_channel():
    return norm_chat_id(os.getenv("FORCE_CHANNEL", ""))

async def make_invite(context, key, ttl=DAY):
    channel = channel_of(key)
    if not channel:
        raise RuntimeError(f"{PRODUCTS[key]['channel_env']} is missing or invalid")
    invite = await context.bot.create_chat_invite_link(
        channel,
        member_limit=1,
        expire_date=datetime.now(timezone.utc) + timedelta(seconds=max(ttl, 60)),
    )
    return invite.invite_link

def link_ttl_text(ttl):
    return f"{max(1, ttl // HOUR)} hours" if ttl >= HOUR else "a few minutes"

def _in_status(status, is_member=None):
    return status in ("creator", "administrator", "member") or (
        status == "restricted" and bool(is_member)
    )

async def is_member(context, channel, uid):
    try:
        member = await context.bot.get_chat_member(channel, uid)
        return _in_status(member.status, getattr(member, "is_member", None))
    except Exception as exc:
        # Fail closed: force-join cannot be verified, so don't silently bypass it.
        log.warning("Force-join check failed: %s", exc)
        return False

JOIN_TEXT = (
    "🔐 <b>Members-Only Access</b>\n\n"
    "<blockquote>Premium services are available to members of our official channel.</blockquote>\n\n"
    "📢 Tap below to join, then return to the bot."
)
JOIN_PROMPTS = {}
LAST_MENU = {}

async def get_force_link(context):
    if _force_link["v"]:
        return _force_link["v"]
    channel = force_channel()
    if not channel:
        return None
    try:
        _force_link["v"] = await context.bot.export_chat_invite_link(channel)
        return _force_link["v"]
    except Exception as exc:
        log.warning("Set FORCE_LINK manually if export fails: %s", exc)
        return None

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

async def ensure_joined(update, context):
    user = update.effective_user
    channel = force_channel()
    if not channel or is_admin(user.id):
        return True
    if await is_member(context, channel, user.id):
        await purge_join_prompts(context.bot, user.id)
        return True
    await purge_join_prompts(context.bot, user.id)
    link = await get_force_link(context)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📢 Join Official Channel", url=link)]]
    ) if link else None
    query = update.callback_query
    if query:
        try:
            await query.answer("Please join the channel first", show_alert=True)
        except Exception:
            pass
    msg = await context.bot.send_message(
        user.id, JOIN_TEXT, reply_markup=keyboard, parse_mode=ParseMode.HTML
    )
    JOIN_PROMPTS.setdefault(user.id, []).append(msg.message_id)
    return False

# ----------------------------- MENUS -----------------------------
def start_text(name, thanks=False):
    """Build the single welcome/start screen using Telegram HTML formatting."""
    name = esc(name or "there")
    if thanks:
        head = (
            f"🎉 <b>Thank you for joining, {name}!</b>\n"
            "✅ Verification complete"
        )
    else:
        head = f"Hey 👋, <b>{name}!</b>\n<b>Welcome To DMT Services</b>"

    return (
        f"{head}\n\n"
        "<blockquote><b>➡️ WHY CHOOSiNG US ❓</b>\n"
        "<i>(Our Speciality)</i>\n\n"
        "• A Seamless Entertainment Hub 📡\n"
        "• Provides Experience ⚡️ &amp; Quality</blockquote>\n\n"
        '🧑‍💻 <b>Developed By:</b> '
        '<a href="https://t.me/DMT_BOTS">DMT BOTS</a>\n\n'
        "👇 <i>Select Option To Continue.</i>"
    )

def home_text(name, thanks=False):
    name = esc(name or "there")
    heading = (
        f"🎉 <b>Thank you for joining, {name}!</b>\n✅ Verification complete\n\n"
        if thanks else "💎 <b>Premium Access Hub</b>\n\n"
    )
    return (
        heading
        + "<blockquote>💎 <b>🚧 Note:</b>\n"
        "• Request Admins For Content ✅\n"
        "• Old + New-Releases 📻💥\n"
        "• Valid For Movies/Web-Series Only. 💈</blockquote>\n\n"
        "<i>- T&C Apply.</i>\n\n"
        "👇 <b>Select Your Plan 👇</b>"
    )
def start_markup(support=True):
    rows = [
        [InlineKeyboardButton("📂 Available Services", callback_data="src")],
        [InlineKeyboardButton("🌐 Terms & Conditions", callback_data="terms")],
    ]
    if support:
        rows.append([InlineKeyboardButton("🛟 Admin Support", url=CONTACT_URL)])
    return InlineKeyboardMarkup(rows)

def home_markup():
    rows = [
        [InlineKeyboardButton(p["name"], callback_data=f"p:{key}")]
        for key, p in PRODUCTS.items()
    ]
    rows += [
        [InlineKeyboardButton("📺 Available OTTs", callback_data="otts")],
        [InlineKeyboardButton("📋 My Subscriptions", callback_data="my")],
        [InlineKeyboardButton("⬅️ Back", callback_data="start")],
    ]
    return InlineKeyboardMarkup(rows)

# ----------------------------- USER COMMANDS -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await touch_user(user)
    if await is_banned(user.id):
        await update.effective_message.reply_text("🚫 You cannot use this bot.")
        return
    if not await ensure_joined(update, context):
        return
    context.user_data.pop("buying", None)
    await drop_last_menu(context.bot, user.id)
    try:
        msg = await update.effective_message.reply_text(
            start_text(user.first_name or user.full_name),
            reply_markup=start_markup(), parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        log.warning("Start screen failed with support button: %s", exc)
        msg = await update.effective_message.reply_text(
            start_text(user.first_name or user.full_name),
            reply_markup=start_markup(support=False), parse_mode=ParseMode.HTML,
        )
    LAST_MENU[user.id] = msg.message_id
    try:
        await update.effective_message.delete()
    except Exception:
        pass

async def availableotts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await touch_user(user)
    if await is_banned(user.id) or not await ensure_joined(update, context):
        return
    await update.effective_message.reply_text(
        AVAILABLE_OTTS_TEXT, parse_mode=ParseMode.HTML
    )

async def my_subs_text(user_id):
    now = int(time.time())
    rows = await db.subs.find({
        "user_id": user_id, "active": True, "expiry_ts": {"$gt": now}
    }).to_list(None)
    if not rows:
        return "You have no active subscriptions."
    lines = ["<b>📋 Your active plans:</b>\n"]
    for row in rows:
        lines.append(
            f"{esc(pname(row['product']))}\n"
            f"Expiry: {fmt(row['expiry_ts'])} ({fmt_left(row['expiry_ts'] - now)} left)"
        )
    return "\n\n".join(lines)

async def show_product(query, key):
    product = PRODUCTS[key]

    if key == "movies":
        description = (
            "🚧 <b>Note:</b>\n\n"
            "<b>• Request Admins For Content ✅</b>\n"
            "<b>• Old + New-Releases 📻💥</b>\n"
            "<b>• Valid For Movies/Web-Series Only.💈</b>\n"
            "<i>-T&C Apply.</i>\n\n"
            "                      <b>Select Your Plan 👇</b>"
        )
    else:
        description = (
            "🚧 <b>Note:</b>\n\n"
            "<b>• Hi-Res (Audio's), WAV, FLAC, DIGI-FLAC , M4A , LOSSLESS✅.</b>\n"
            "<b>• Old + New-Releases ALL 📻💥.</b>\n"
            "<b>• CD-Rips , WAV ,Digital Dolby 5.1 % 7.1 QLTY Only For Movies.</b>\n\n"
            "<blockquote><b>• Why Audio Quality Matters</b>\n"
            "<i>• On Expensive/Big Sound Systems, Low-Quality Files Lose Their Punch, Bass Turns Muddy, Highs Sounds Dull, and The Whole Track Feels Flat. High-Quality Audio Keeps The Full Bass, Mid and High Range Intact, Giving Your Set More Energy, Clarity and Presence.</i>\n"
            "<b>• Only 1 File Provided For Quality Check Sample.</b></blockquote>\n\n"
            "<i>-T&C Apply.</i>\n\n"
            "                      <b>Select Your Plan 👇</b>"
        )
    text = f"🎗 <b>VIP ACCESS PLANS</b> 🎗\n<b>{esc(product['name'])}</b>\n\n{description}"
    rows = [
        [InlineKeyboardButton(
            f"{plan['label']} — ₹{plan['price']}",
            callback_data=f"plan:{key}:{plan['id']}"
        )]
        for plan in PLANS
    ]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML
    )

async def show_payment(query, context, key, pid):
    plan = get_plan(pid)
    if not plan:
        await query.answer("Invalid plan", show_alert=True)
        return
    context.user_data["buying"] = {"key": key, "plan": pid}
    has_qr = bool(await get_setting("qr_file_id"))
    text = (
        f"<b>{esc(PRODUCTS[key]['name'])}</b>\n"
        f"📦 Plan: <b>{plan['label']} — ₹{plan['price']}</b>\n\n"
        "<b>Make payment</b>\n\n"
        f"UPI ID (tap to copy):\n<code>{esc(UPI_ID)}</code>\n\n"
    )
    rows = []
    if has_qr:
        text += "Or scan the QR code below.\n\n"
        rows.append([InlineKeyboardButton("📷 Show QR Code", callback_data="qr")])
    text += "After payment, send the screenshot here as a photo."
    rows.append([InlineKeyboardButton("⬅️ Back / Cancel", callback_data=f"p:{key}")])
    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML
    )

async def user_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    if await is_banned(uid):
        await query.answer("🚫 You are banned", show_alert=True)
        return
    if not await ensure_joined(update, context):
        return
    data = query.data
    if data == "qr":
        file_id = await get_setting("qr_file_id")
        if not file_id:
            await query.answer("QR unavailable; use UPI ID.", show_alert=True)
            return
        await query.answer()
        caption = "📷 Scan the QR to pay."
        buying = context.user_data.get("buying")
        if buying:
            plan = get_plan(buying["plan"])
            if plan:
                caption += f"\n{plan['label']} — ₹{plan['price']}"
        caption += "\nSend payment screenshot here after paying."
        await context.bot.send_photo(uid, file_id, caption=caption)
        return
    await query.answer()
    await touch_user(query.from_user)
    if data in ("home", "src"):
        context.user_data.pop("buying", None)
        await query.edit_message_text(
            home_text(query.from_user.first_name or query.from_user.full_name),
            reply_markup=home_markup(), parse_mode=ParseMode.HTML
        )
    elif data == "start":
        context.user_data.pop("buying", None)
        await query.edit_message_text(
            start_text(query.from_user.first_name or query.from_user.full_name),
            reply_markup=start_markup(), parse_mode=ParseMode.HTML
        )
    elif data == "terms":
        await query.edit_message_text(
            TERMS_TEXT,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="start")
            ]]),
            parse_mode=ParseMode.HTML,
        )
    elif data == "otts":
        await context.bot.send_message(uid, AVAILABLE_OTTS_TEXT, parse_mode=ParseMode.HTML)
    elif data == "my":
        await query.edit_message_text(
            await my_subs_text(uid),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="home")
            ]]),
            parse_mode=ParseMode.HTML,
        )
    elif data.startswith("p:"):
        key = data[2:]
        if key in PRODUCTS:
            context.user_data.pop("buying", None)
            await show_product(query, key)
    elif data.startswith("plan:"):
        parts = data.split(":")
        if len(parts) == 3 and parts[1] in PRODUCTS:
            await show_payment(query, context, parts[1], parts[2])

async def mysubs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await touch_user(user)
    if await is_banned(user.id) or not await ensure_joined(update, context):
        return
    await update.effective_message.reply_text(
        await my_subs_text(user.id), parse_mode=ParseMode.HTML
    )

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if await is_banned(user.id):
        return
    await touch_user(user)
    if not await ensure_joined(update, context):
        return
    buying = context.user_data.get("buying")
    plan = get_plan(buying["plan"]) if buying else None
    if not buying or not plan or buying.get("key") not in PRODUCTS:
        await update.effective_message.reply_text("Please use /start and choose a plan first.")
        return
    key = buying["key"]
    request_id = await next_req_id()
    file_id = update.effective_message.photo[-1].file_id
    await db.requests.insert_one({
        "_id": request_id, "user_id": user.id, "username": user.username,
        "full_name": user.full_name, "product": key, "status": "pending",
        "file_id": file_id, "created_at": int(time.time()),
        "plan_secs": plan["secs"], "plan_label": plan["label"],
        "plan_price": plan["price"],
    })
    context.user_data.pop("buying", None)
    await update.effective_message.reply_text(
        "✅ Screenshot received. Admin will verify the payment."
    )
    caption = (
        f"🧾 <b>Payment Request #{request_id}</b>\n\n"
        f"👤 {esc(user.full_name)} (@{esc(user.username) or '-'})\n"
        f"🆔 <code>{user.id}</code>\n"
        f"📦 {esc(PRODUCTS[key]['name'])}\n"
        f"⏳ Plan: {esc(plan['label'])} — ₹{plan['price']}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"✅ Approve ({fmt_dur(plan['secs'])})", callback_data=f"ap:{request_id}"
        )],
        [
            InlineKeyboardButton("⏱ Change duration", callback_data=f"chg:{request_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"rj:{request_id}"),
        ],
    ])
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                admin_id, file_id, caption=caption, reply_markup=keyboard,
                parse_mode=ParseMode.HTML, auto_delete=False,
            )
        except Exception:
            log.exception("Could not notify admin %s", admin_id)

# ----------------------------- APPROVAL -----------------------------
def duration_keyboard(request_id):
    rows, row = [], []
    for seconds, label in DURATION_CHOICES:
        row.append(InlineKeyboardButton(label, callback_data=f"dur:{request_id}:{seconds}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✏️ Custom days", callback_data=f"cd:{request_id}")])
    rows.append([InlineKeyboardButton("❌ Reject", callback_data=f"rj:{request_id}")])
    return InlineKeyboardMarkup(rows)

async def finalize(context, request_id, seconds):
    """Atomically claim request, create/extend subscription, then notify user."""
    now = int(time.time())
    req = await db.requests.find_one_and_update(
        {"_id": request_id, "status": "pending"},
        {"$set": {"status": "processing", "processing_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if not req:
        return "⚠️ Request already handled or is being processed."
    try:
        key, uid = req["product"], req["user_id"]
        if key not in PRODUCTS or seconds <= 0:
            raise ValueError("Invalid product or duration")
        sub_id = f"{uid}:{key}"
        sub = await db.subs.find_one({"_id": sub_id})
        if sub and sub.get("active") and sub.get("expiry_ts", 0) > now:
            start_ts, base = sub.get("start_ts", now), sub["expiry_ts"]
        else:
            start_ts, base = now, now
        expiry = base + seconds
        reminded = expiry - now <= REMIND_DAYS * DAY
        await db.subs.update_one(
            {"_id": sub_id},
            {"$set": {
                "user_id": uid, "product": key, "start_ts": start_ts,
                "expiry_ts": expiry, "active": True, "reminded": reminded,
            }},
            upsert=True,
        )
        try:
            ttl = min(DAY, expiry - now)
            link = await make_invite(context, key, ttl)
            message = (
                f"✅ <b>Payment approved!</b>\n\n📦 {esc(pname(key))}\n"
                f"🗓 Starts: {fmt(start_ts)}\n⏳ Expiry: <b>{fmt(expiry)}</b>\n\n"
                f"🔗 One-use invite link (valid for {esc(link_ttl_text(ttl))}):\n{esc(link)}"
            )
        except Exception as exc:
            log.exception("Invite link creation failed")
            message = (
                f"✅ <b>Payment approved!</b>\n\n📦 {esc(pname(key))}\n"
                f"⏳ Expiry: <b>{fmt(expiry)}</b>\n\n"
                "⚠️ Your subscription is saved, but the invite link could not be created. "
                "Please contact admin."
            )
        try:
            await context.bot.send_message(uid, message, parse_mode=ParseMode.HTML, auto_delete=False)
        except Exception:
            log.exception("Could not notify approved user %s", uid)
        await db.requests.update_one(
            {"_id": request_id, "status": "processing"},
            {"$set": {"status": "approved", "handled_at": int(time.time()),
                      "approved_secs": seconds}},
        )
        return f"✅ <b>Approved</b> — {esc(fmt_dur(seconds))}, expiry {fmt(expiry)}"
    except Exception:
        log.exception("Approval failed for request #%s", request_id)
        await db.requests.update_one(
            {"_id": request_id, "status": "processing"},
            {"$set": {"status": "pending", "last_error_at": int(time.time())}},
        )
        return "❌ Approval failed. Request returned to pending; check Render logs."

async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Admins only", show_alert=True)
        return
    await query.answer()
    parts = query.data.split(":")
    try:
        action, request_id = parts[0], int(parts[1])
    except (ValueError, IndexError):
        return
    req = await db.requests.find_one({"_id": request_id})
    if not req:
        await query.message.reply_text("Request not found.")
        return
    if action == "chg":
        if req["status"] == "pending":
            await query.edit_message_reply_markup(duration_keyboard(request_id))
        return
    if action == "cd":
        if req["status"] != "pending":
            await query.message.reply_text("Request already handled.")
            return
        context.user_data["await_days"] = request_id
        await query.message.reply_text(
            f"Request #{request_id}: send duration in days (1-3650)."
        )
        return
    if action == "rj":
        result = await db.requests.update_one(
            {"_id": request_id, "status": "pending"},
            {"$set": {"status": "rejected", "handled_at": int(time.time())}},
        )
        if result.modified_count:
            try:
                await context.bot.send_message(
                    req["user_id"],
                    "❌ Payment could not be verified. Please contact support or try again.",
                    auto_delete=False,
                )
            except Exception:
                pass
            try:
                await query.edit_message_caption(
                    (query.message.caption_html or "") + "\n\n❌ <b>Rejected</b>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        else:
            await query.message.reply_text("Request already handled.")
        return
    if action in ("ap", "dur"):
        seconds = int(req["plan_secs"]) if action == "ap" else int(parts[2])
        summary = await finalize(context, request_id, seconds)
        try:
            await query.edit_message_caption(
                (query.message.caption_html or "") + "\n\n" + summary,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await query.message.reply_text(summary, parse_mode=ParseMode.HTML)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_admin(user.id) and context.user_data.get("await_days"):
        value = (update.effective_message.text or "").strip()
        if not value.isdigit() or not 1 <= int(value) <= 3650:
            await update.effective_message.reply_text("Send a valid number from 1 to 3650.")
            return
        request_id = context.user_data.pop("await_days")
        summary = await finalize(context, request_id, int(value) * DAY)
        await update.effective_message.reply_text(summary, parse_mode=ParseMode.HTML)
        return
    if not await is_banned(user.id):
        await touch_user(user)
        if await ensure_joined(update, context):
            await update.effective_message.reply_text("Use /start to open the menu.")

# ----------------------------- ADMIN COMMANDS -----------------------------
async def subs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    now = int(time.time())
    rows = await db.subs.find({"active": True, "expiry_ts": {"$gt": now}}).to_list(None)
    rows.sort(key=lambda item: item["expiry_ts"])
    if not rows:
        await update.effective_message.reply_text("No active subscribers.")
        return
    users = {u["_id"]: u for u in await db.users.find({}).to_list(None)}
    lines = [f"<b>Active subscribers ({len(rows)})</b>\n"]
    for row in rows:
        user = users.get(row["user_id"], {})
        lines.append(
            f"• {esc(user.get('full_name'))} (@{esc(user.get('username')) or '-'}) "
            f"<code>{row['user_id']}</code>\n"
            f"  {esc(pname(row['product']))} → {fmt(row['expiry_ts'])}"
        )
    output = "\n".join(lines)
    for start in range(0, len(output), 3500):
        await update.effective_message.reply_text(
            output[start:start + 3500], parse_mode=ParseMode.HTML
        )

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    now = int(time.time())
    users = await db.users.count_documents({})
    pending = await db.requests.count_documents({"status": "pending"})
    banned = await db.banned.count_documents({})
    approved = await db.requests.find({"status": "approved"}).to_list(None)
    paid_sum = sum(row.get("plan_price", 0) or 0 for row in approved)
    active = await db.subs.find({"active": True, "expiry_ts": {"$gt": now}}).to_list(None)
    per_product = {}
    for sub in active:
        per_product[sub["product"]] = per_product.get(sub["product"], 0) + 1
    lines = [
        f"👥 Total users: {users}", f"🚫 Banned: {banned}",
        f"⏳ Pending requests: {pending}",
        f"💰 Approved payments: {len(approved)} (₹{paid_sum})", "", "Active subscriptions:",
    ]
    lines += [f"• {pname(key)}: {count}" for key, count in per_product.items()] or ["• none"]
    await update.effective_message.reply_text("\n".join(lines))

async def kick(context, uid, key):
    channel = channel_of(key)
    if not channel:
        raise RuntimeError(f"Channel for {key} is not configured")
    await context.bot.ban_chat_member(channel, uid)
    await context.bot.unban_chat_member(channel, uid, only_if_banned=True)

async def revoke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if len(args) != 2 or args[1] not in PRODUCTS or not args[0].isdigit():
        await update.effective_message.reply_text(
            "Use: /revoke <user_id> <product>\nProducts: " + ", ".join(PRODUCTS)
        )
        return
    uid, key = int(args[0]), args[1]
    try:
        await kick(context, uid, key)
        await db.subs.update_one({"_id": f"{uid}:{key}"}, {"$set": {"active": False}})
        await update.effective_message.reply_text("Revoked successfully ✅")
    except Exception as exc:
        log.exception("Revoke failed")
        await update.effective_message.reply_text(f"❌ Channel removal failed: {esc(exc)}")

async def resend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if len(args) != 2 or args[1] not in PRODUCTS or not args[0].isdigit():
        await update.effective_message.reply_text(
            "Use: /resend <user_id> <product>"
        )
        return
    uid, key = int(args[0]), args[1]
    now = int(time.time())
    sub = await db.subs.find_one({
        "_id": f"{uid}:{key}", "active": True, "expiry_ts": {"$gt": now}
    })
    if not sub:
        await update.effective_message.reply_text("User has no active subscription for this product.")
        return
    try:
        ttl = min(DAY, sub["expiry_ts"] - now)
        link = await make_invite(context, key, ttl)
        await context.bot.send_message(
            uid, f"🔗 {esc(pname(key))} invite link (one use, {esc(link_ttl_text(ttl))}):\n{esc(link)}",
            parse_mode=ParseMode.HTML, auto_delete=False,
        )
        await update.effective_message.reply_text("Link sent ✅")
    except Exception as exc:
        log.exception("Resend failed")
        await update.effective_message.reply_text(f"❌ Failed: {esc(exc)}")

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Use: /ban <user_id>")
        return
    uid = int(context.args[0])
    if is_admin(uid):
        await update.effective_message.reply_text("Cannot ban an admin.")
        return
    await db.banned.update_one(
        {"_id": uid}, {"$setOnInsert": {"at": int(time.time())}}, upsert=True
    )
    await db.subs.update_many({"user_id": uid}, {"$set": {"active": False}})
    lines = [f"🚫 {uid} banned from bot."]
    for channel in all_channels():
        try:
            await context.bot.ban_chat_member(channel, uid)
            lines.append(f"✅ Channel {channel}: banned")
        except Exception as exc:
            lines.append(f"❌ Channel {channel}: {esc(exc)}")
    await update.effective_message.reply_text("\n".join(lines))

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Use: /unban <user_id>")
        return
    uid = int(context.args[0])
    await db.banned.delete_one({"_id": uid})
    lines = [f"✅ {uid} unbanned from bot."]
    for channel in all_channels():
        try:
            await context.bot.unban_chat_member(channel, uid, only_if_banned=True)
            lines.append(f"✅ Channel {channel}: unbanned")
        except Exception as exc:
            lines.append(f"❌ Channel {channel}: {esc(exc)}")
    await update.effective_message.reply_text("\n".join(lines))

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    message = update.effective_message
    reply = message.reply_to_message
    parts = (message.text or "").split(maxsplit=1)
    body = parts[1] if len(parts) > 1 else ""
    if not reply and not body:
        await message.reply_text(
            "Use /broadcast <message>, or reply to a message with /broadcast."
        )
        return
    banned_ids = {doc["_id"] for doc in await db.banned.find({}).to_list(None)}
    user_ids = [
        doc["_id"] for doc in await db.users.find({}).to_list(None)
        if doc["_id"] not in banned_ids
    ]
    await message.reply_text(f"📢 Broadcast starting: {len(user_ids)} users...")
    admin_id = update.effective_user.id

    async def run_broadcast():
        sent = failed = 0
        for uid in user_ids:
            try:
                if reply:
                    await context.bot.copy_message(uid, reply.chat_id, reply.message_id)
                else:
                    await context.bot.send_message(uid, body, auto_delete=False)
                sent += 1
            except RetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
                try:
                    if reply:
                        await context.bot.copy_message(uid, reply.chat_id, reply.message_id)
                    else:
                        await context.bot.send_message(uid, body, auto_delete=False)
                    sent += 1
                except Exception:
                    failed += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
        await context.bot.send_message(
            admin_id, f"📢 Broadcast finished\nSent: {sent}\nFailed: {failed}",
            auto_delete=False,
        )
    context.application.create_task(run_broadcast())

async def setqr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    reply = update.effective_message.reply_to_message
    if not reply or not reply.photo:
        await update.effective_message.reply_text(
            "Reply to a QR photo with /setqr."
        )
        return
    await set_setting("qr_file_id", reply.photo[-1].file_id)
    await update.effective_message.reply_text("✅ QR saved.")

async def removeqr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await db.settings.delete_one({"_id": "qr_file_id"})
    await update.effective_message.reply_text("QR removed.")

async def forwarded_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    origin = getattr(update.effective_message, "forward_origin", None)
    chat = getattr(origin, "chat", None)
    if not chat:
        await update.effective_message.reply_text("Please forward a message from a channel.")
        return
    await update.effective_message.reply_text(f"📡 {chat.title}\nID: {chat.id}")

async def forcecheck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    raw = os.getenv("FORCE_CHANNEL")
    channel = force_channel()
    if not channel:
        await update.effective_message.reply_text(
            f"❌ FORCE_CHANNEL missing/invalid (current value: {raw!r})."
        )
        return
    lines = [f"FORCE_CHANNEL: {channel}"]
    try:
        chat = await context.bot.get_chat(channel)
        lines.append(f"Channel: {chat.title}")
        me = await context.bot.get_chat_member(channel, context.bot.id)
        lines.append(f"Bot status: {me.status}")
        lines.append("✅ Bot admin." if me.status == "administrator"
                     else "⚠️ Make the bot an admin in the force-join channel.")
    except Exception as exc:
        lines.append(f"❌ Error: {esc(exc)}")
    lines.append(f"Join link: {await get_force_link(context) or 'Set FORCE_LINK manually'}")
    await update.effective_message.reply_text("\n".join(lines))

async def on_force_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    member_update = update.chat_member
    channel = force_channel()
    if not member_update or not channel or member_update.chat.id != channel:
        return
    user = member_update.new_chat_member.user
    if user.is_bot:
        return
    was_member = _in_status(
        member_update.old_chat_member.status,
        getattr(member_update.old_chat_member, "is_member", None)
    )
    now_member = _in_status(
        member_update.new_chat_member.status,
        getattr(member_update.new_chat_member, "is_member", None)
    )
    if await db.users.find_one({"_id": user.id}) is None or await is_banned(user.id):
        return
    try:
        if now_member and not was_member:
            await purge_join_prompts(context.bot, user.id)
            await drop_last_menu(context.bot, user.id)
            msg = await context.bot.send_message(
                user.id, start_text(user.first_name or user.full_name, thanks=True),
                reply_markup=start_markup(), parse_mode=ParseMode.HTML,
            )
            LAST_MENU[user.id] = msg.message_id
        elif was_member and not now_member:
            await context.bot.send_message(
                user.id,
                "👋 You left the official channel. Join again to use the bot.",
                parse_mode=ParseMode.HTML,
            )
    except Exception:
        log.exception("Force-channel membership notification failed for %s", user.id)

# ----------------------------- EXPIRY JOB -----------------------------
async def expiry_job(context: ContextTypes.DEFAULT_TYPE):
    now = int(time.time())
    to_remind = await db.subs.find({
        "active": True, "reminded": False,
        "expiry_ts": {"$gt": now, "$lte": now + REMIND_DAYS * DAY},
    }).to_list(None)
    for sub in to_remind:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Renew", callback_data=f"p:{sub['product']}")
        ]])
        try:
            await context.bot.send_message(
                sub["user_id"],
                f"⚠️ Your <b>{esc(pname(sub['product']))}</b> subscription expires "
                f"on <b>{fmt(sub['expiry_ts'])}</b>. Please renew.",
                reply_markup=keyboard, parse_mode=ParseMode.HTML, auto_delete=False,
            )
        except Exception:
            log.exception("Expiry reminder failed for user %s", sub["user_id"])
        await db.subs.update_one(
            {"_id": sub["_id"], "expiry_ts": sub["expiry_ts"], "reminded": False},
            {"$set": {"reminded": True}},
        )

    expired = await db.subs.find({
        "active": True, "expiry_ts": {"$lte": now}
    }).to_list(None)
    for sub in expired:
        result = await db.subs.update_one(
            {"_id": sub["_id"], "expiry_ts": sub["expiry_ts"], "active": True},
            {"$set": {"active": False}},
        )
        if not result.modified_count:
            continue
        try:
            await kick(context, sub["user_id"], sub["product"])
        except Exception:
            # Subscription is marked inactive; explicitly alert admins if removal failed.
            log.exception("Could not remove expired user %s", sub["user_id"])
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        admin_id,
                        f"⚠️ Expired subscription removal failed for user {sub['user_id']} "
                        f"({esc(pname(sub['product']))}). Remove manually.",
                        auto_delete=False,
                    )
                except Exception:
                    pass
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Renew subscription", callback_data=f"p:{sub['product']}")
        ]])
        try:
            await context.bot.send_message(
                sub["user_id"],
                f"⏰ Your <b>{esc(pname(sub['product']))}</b> subscription has expired.\n"
                "Renew to request access again.",
                reply_markup=keyboard, parse_mode=ParseMode.HTML, auto_delete=False,
            )
        except Exception:
            pass

# ----------------------------- HEALTH SERVER / MAIN -----------------------------
def start_health_server():
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
    log.info("Health server listening on port %s", port)

def main():
    start_health_server()
    app = (
        Application.builder()
        .bot(PremiumBot(BOT_TOKEN))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mysubs", mysubs_cmd))
    app.add_handler(CommandHandler("availableotts", availableotts_cmd))
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
    app.add_handler(CallbackQueryHandler(
        admin_cb, pattern=r"^(ap|chg|cd|dur|rj):"
    ))
    app.add_handler(CallbackQueryHandler(
        user_cb, pattern=r"^(home|src|start|terms|my|otts|qr|p:.*|plan:.*)$"
    ))
    app.add_handler(ChatMemberHandler(on_force_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, on_photo))
    app.add_handler(MessageHandler(
        filters.FORWARDED & filters.ChatType.PRIVATE, forwarded_id
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text
    ))
    app.job_queue.run_repeating(expiry_job, interval=60, first=15)
    log.info("Bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
