from pathlib import Path
import zipfile

out = Path("/mnt/data/premium_subscription_bot")
out.mkdir(parents=True, exist_ok=True)

main_py = r'''"""Premium Subscription Telegram Bot.
Flow: user selects product/plan -> pays by UPI/QR -> submits screenshot ->
admin approves -> bot sends a single-use invite link -> bot revokes access at expiry.

Required environment variables:
BOT_TOKEN, MONGO_URI, ADMIN_IDS, CH_MOVIES, CH_MUSIC
Optional: MONGO_DB, UPI_ID, CONTACT_URL
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
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("premium-sub-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
MONGO_URI = os.environ["MONGO_URI"].strip()
MONGO_DB = os.getenv("MONGO_DB", "subbot").strip()
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(",", " ").split()
    if x.isdigit()
}
UPI_ID = os.getenv("UPI_ID", "yourname@upi").strip()
CONTACT_URL = os.getenv("CONTACT_URL", "https://t.me").strip()

IST = ZoneInfo("Asia/Kolkata")
HOUR, DAY = 3600, 86400
REMIND_DAYS = 2

AVAILABLE_OTTS_TEXT = """📌 AVAILABLE OTTs & Services
Alt Balaji 🅰️
Aha 🔶
Arey Digital 🎬
Amazon Mini TV 📺
Bongo TV 🌐
Dangal Play 🟣
Discovery+ 🎱
JioHotstar 💠
JioCinema 🌸
Netflix 🧧
Pocket FM 🀄
Prime Video 🔵
STAGE 🌀
ShemarooMe 📽️
SonyLIV 🟣
Sun NXT 🎃
ZEE5 ♈

📌 Total = 17 OTTs

👾 Daily Shows 👾
TV Shows, Colors, Dangal Play, JioHotstar, SonyLIV, Sun NXT, ZEE5

Examples:
Taarak Mehta, Bigg Boss, New CID S03, Pushpa Impossible,
Kumkum Bhagya, other ongoing shows

⚡ Weekly releases, reality shows, Laughter Chefs,
India's Got Talent and other shows"""

PLANS = [
    {"id": "60d", "label": "🥇 60 Days", "secs": 60 * DAY, "price": 79},
    {"id": "30d", "label": "🥈 30 Days", "secs": 30 * DAY, "price": 39},
    {"id": "7d", "label": "🥉 7 Days", "secs": 7 * DAY, "price": 19},
    {"id": "3h", "label": "⚜️ 3 Hours", "secs": 3 * HOUR, "price": 9},
]

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

DURATION_CHOICES = [
    (3 * HOUR, "3h"), (7 * DAY, "7d"), (15 * DAY, "15d"),
    (30 * DAY, "30d"), (60 * DAY, "60d"), (90 * DAY, "90d"),
    (180 * DAY, "180d"), (365 * DAY, "365d"),
]

client = None
db = None


def esc(value):
    return html.escape(str(value) if value is not None else "")


def fmt(ts):
    return datetime.fromtimestamp(int(ts), IST).strftime("%d %b %Y, %I:%M %p")


def fmt_dur(seconds):
    if seconds < DAY:
        return f"{max(1, seconds // HOUR)} hour(s)"
    return f"{max(1, seconds // DAY)} day(s)"


def fmt_left(seconds):
    if seconds >= DAY:
        return f"{seconds // DAY} day(s)"
    if seconds >= HOUR:
        return f"{seconds // HOUR} hour(s)"
    return f"{max(1, seconds // 60)} minute(s)"


def get_plan(pid):
    return next((plan for plan in PLANS if plan["id"] == pid), None)


def pname(key):
    return PRODUCTS.get(key, {}).get("name", key)


def is_admin(user_id):
    return user_id in ADMIN_IDS


def channel_of(key):
    if key not in PRODUCTS:
        return None
    raw = os.getenv(PRODUCTS[key]["channel_env"], "").strip().strip("\"'")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def all_channels():
    return sorted({channel_of(key) for key in PRODUCTS if channel_of(key)})


async def post_init(app):
    global client, db
    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS missing. Set one or more numeric Telegram user IDs.")
    client = AsyncMongoClient(MONGO_URI, serverSelectionTimeoutMS=15000)
    db = client[MONGO_DB]
    await client.admin.command("ping")
    await db.requests.create_index([("status", 1), ("created_at", -1)])
    await db.subs.create_index([("user_id", 1), ("active", 1), ("expiry_ts", 1)])
    await db.users.create_index([("last_seen", -1)])
    log.info("MongoDB connected (db=%s)", MONGO_DB)


async def post_shutdown(app):
    if client is not None:
        await client.close()


async def next_req_id():
    doc = await db.counters.find_one_and_update(
        {"_id": "requests"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int(doc["seq"])


async def get_setting(key):
    record = await db.settings.find_one({"_id": key})
    return record.get("value") if record else None


async def set_setting(key, value):
    await db.settings.update_one(
        {"_id": key}, {"$set": {"value": value}}, upsert=True
    )


async def touch_user(user):
    now = int(time.time())
    await db.users.update_one(
        {"_id": user.id},
        {"$set": {
            "username": user.username,
            "full_name": user.full_name,
            "last_seen": now,
        }, "$setOnInsert": {"first_seen": now}},
        upsert=True,
    )


async def is_banned(user_id):
    if is_admin(user_id):
        return False
    return await db.banned.find_one({"_id": user_id}) is not None


async def make_invite(context, product_key, ttl):
    channel_id = channel_of(product_key)
    if not channel_id:
        raise RuntimeError(
            f"{PRODUCTS[product_key]['channel_env']} environment variable missing/invalid"
        )
    expires = datetime.now(timezone.utc) + timedelta(seconds=max(60, int(ttl)))
    invite = await context.bot.create_chat_invite_link(
        chat_id=channel_id,
        member_limit=1,
        expire_date=expires,
    )
    return invite.invite_link


async def kick(context, user_id, product_key):
    channel_id = channel_of(product_key)
    if not channel_id:
        log.error("Cannot remove user: channel ID missing for %s", product_key)
        return False
    try:
        # Temporary ban then unban removes the member but allows future rejoining.
        await context.bot.ban_chat_member(channel_id, user_id)
        await context.bot.unban_chat_member(channel_id, user_id, only_if_banned=True)
        return True
    except TelegramError as exc:
        log.warning("Could not remove %s from %s: %s", user_id, product_key, exc)
        return False


def home_markup():
    rows = [
        [InlineKeyboardButton(product["name"], callback_data=f"p:{key}")]
        for key, product in PRODUCTS.items()
    ]
    rows.append([InlineKeyboardButton("📋 My Subscriptions", callback_data="my")])
    rows.append([InlineKeyboardButton("💬 Contact Admin", url=CONTACT_URL)])
    return InlineKeyboardMarkup(rows)


HOME_TEXT = "👋 <b>Welcome to Premium Access!</b>\n\nNeeche se product aur plan choose karein 👇"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.effective_message:
        return
    await touch_user(user)
    if await is_banned(user.id):
        await update.effective_message.reply_text("🚫 Aap is bot ko use nahi kar sakte.")
        return
    context.user_data.pop("buying", None)
    await update.effective_message.reply_text(
        HOME_TEXT, reply_markup=home_markup(), parse_mode=ParseMode.HTML
    )


async def availableotts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    await touch_user(user)
    if await is_banned(user.id):
        return
    await update.effective_message.reply_text(AVAILABLE_OTTS_TEXT)


async def show_product(query, key):
    product = PRODUCTS[key]
    text = (
        f"🎗 <b>VIP ACCESS PLANS</b> 🎗\n<b>{esc(product['name'])}</b>\n\n"
        f"📜 <b>Terms:</b>\n{esc(product['terms'])}\n\n"
        "👇 Apna plan select karein"
    )
    buttons = [
        [InlineKeyboardButton(
            f"{plan['label']} — ₹{plan['price']}",
            callback_data=f"plan:{key}:{plan['id']}",
        )]
        for plan in PLANS
    ]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML
    )


async def show_payment(query, context, key, plan_id):
    plan = get_plan(plan_id)
    if not plan:
        await query.answer("Invalid plan.", show_alert=True)
        return
    context.user_data["buying"] = {"key": key, "plan": plan_id}
    has_qr = bool(await get_setting("qr_file_id"))
    text = (
        f"<b>{esc(PRODUCTS[key]['name'])}</b>\n"
        f"📦 Plan: <b>{esc(plan['label'])} — ₹{plan['price']}</b>\n\n"
        "<b>Payment karein</b>\n\n"
        f"UPI ID (copy karein):\n<code>{esc(UPI_ID)}</code>\n\n"
    )
    buttons = []
    if has_qr:
        text += "Ya QR scan karein 👇\n\n"
        buttons.append([InlineKeyboardButton("📷 Show QR Code", callback_data="qr")])
    text += "Payment ke baad isi chat mein payment screenshot (photo) bhejein."
    buttons.append([InlineKeyboardButton("⬅️ Back / Cancel", callback_data=f"p:{key}")])
    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML
    )


async def my_subs_text(user_id):
    now = int(time.time())
    subscriptions = await db.subs.find({
        "user_id": user_id, "active": True, "expiry_ts": {"$gt": now}
    }).to_list(length=None)
    if not subscriptions:
        return "Aapka koi active subscription nahi hai."
    lines = ["<b>📋 Aapke active subscriptions:</b>\n"]
    for sub in subscriptions:
        lines.append(
            f"{esc(pname(sub['product']))}\n"
            f"Expiry: {fmt(sub['expiry_ts'])} "
            f"({fmt_left(sub['expiry_ts'] - now)} baaki)"
        )
    return "\n\n".join(lines)


async def user_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    if await is_banned(user.id):
        await query.answer("🚫 Aap banned hain.", show_alert=True)
        return
    data = query.data or ""

    if data == "qr":
        file_id = await get_setting("qr_file_id")
        if not file_id:
            await query.answer("QR abhi available nahi hai. UPI use karein.", show_alert=True)
            return
        await query.answer()
        buying = context.user_data.get("buying")
        caption = "📷 QR scan karke payment karein."
        if buying:
            plan = get_plan(buying.get("plan"))
            if plan:
                caption += f"\n{plan['label']} — ₹{plan['price']}"
        caption += "\n\nPayment ke baad isi chat mein screenshot bhejein."
        await context.bot.send_photo(chat_id=user.id, photo=file_id, caption=caption)
        return

    await query.answer()
    await touch_user(user)
    if data == "home":
        context.user_data.pop("buying", None)
        await query.edit_message_text(
            HOME_TEXT, reply_markup=home_markup(), parse_mode=ParseMode.HTML
        )
    elif data == "my":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data="home")]
        ])
        await query.edit_message_text(
            await my_subs_text(user.id),
            reply_markup=keyboard,
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
    if not user:
        return
    await touch_user(user)
    if await is_banned(user.id):
        return
    await update.effective_message.reply_text(
        await my_subs_text(user.id), parse_mode=ParseMode.HTML
    )


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if await is_banned(user.id):
        return
    await touch_user(user)
    buying = context.user_data.get("buying")
    plan = get_plan(buying.get("plan")) if buying else None
    key = buying.get("key") if buying else None
    if not plan or key not in PRODUCTS:
        await message.reply_text("Pehle /start karke product aur plan choose karein.")
        return

    request_id = await next_req_id()
    file_id = message.photo[-1].file_id
    await db.requests.insert_one({
        "_id": request_id,
        "user_id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "product": key,
        "status": "pending",
        "file_id": file_id,
        "created_at": int(time.time()),
        "plan_secs": plan["secs"],
        "plan_label": plan["label"],
        "plan_price": plan["price"],
    })
    context.user_data.pop("buying", None)
    await message.reply_text(
        f"✅ Screenshot mil gaya! Payment request #{request_id} admin verification ke liye bhej di hai."
    )

    caption = (
        f"🧾 <b>New Payment Request #{request_id}</b>\n\n"
        f"👤 {esc(user.full_name)} (@{esc(user.username) or '-'})\n"
        f"🆔 <code>{user.id}</code>\n"
        f"📦 {esc(PRODUCTS[key]['name'])}\n"
        f"⏳ Plan: {esc(plan['label'])} — ₹{plan['price']}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"✅ Approve ({fmt_dur(plan['secs'])})",
            callback_data=f"ap:{request_id}",
        )],
        [
            InlineKeyboardButton("⏱ Change duration", callback_data=f"chg:{request_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"rj:{request_id}"),
        ],
    ])
    delivered = 0
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                chat_id=admin_id, photo=file_id, caption=caption,
                reply_markup=keyboard, parse_mode=ParseMode.HTML,
            )
            delivered += 1
        except TelegramError as exc:
            log.warning("Could not notify admin %s: %s", admin_id, exc)
    if delivered == 0:
        log.error("Payment request %s saved but no admin notification was delivered", request_id)


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
    """Approve once, extend an existing active subscription, and send an invite."""
    if seconds <= 0 or seconds > 3650 * DAY:
        return "❌ Invalid duration."
    now = int(time.time())
    request = await db.requests.find_one_and_update(
        {"_id": request_id, "status": "pending"},
        {"$set": {"status": "processing", "processing_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if not request:
        return "Request pehle hi handle ho chuki hai."

    key, user_id = request["product"], request["user_id"]
    sub_id = f"{user_id}:{key}"
    sub = await db.subs.find_one({"_id": sub_id})
    if sub and sub.get("active") and int(sub.get("expiry_ts", 0)) > now:
        start_ts = int(sub.get("start_ts", now))
        base = int(sub["expiry_ts"])
    else:
        start_ts, base = now, now
    expiry = base + seconds
    reminded = (expiry - now) <= REMIND_DAYS * DAY
    await db.subs.update_one(
        {"_id": sub_id},
        {"$set": {
            "user_id": user_id, "product": key, "start_ts": start_ts,
            "expiry_ts": expiry, "active": True, "reminded": reminded,
            "updated_at": now,
        }},
        upsert=True,
    )
    await db.requests.update_one(
        {"_id": request_id, "status": "processing"},
        {"$set": {
            "status": "approved", "approved_at": now,
            "approved_secs": seconds, "expiry_ts": expiry,
        }},
    )

    text = (
        "✅ <b>Payment approved!</b>\n\n"
        f"📦 {esc(pname(key))}\n"
        f"🗓 Subscription start: {fmt(start_ts)}\n"
        f"⏳ Expiry: {fmt(expiry)}\n\n"
    )
    note = ""
    try:
        ttl = min(DAY, max(60, expiry - now))
        invite_url = await make_invite(context, key, ttl)
        text += (
            f"🔗 <b>Join link</b> (1 use, approx. {esc(link_ttl_text(ttl))} valid):\n"
            f"{esc(invite_url)}"
        )
    except Exception as exc:
        log.exception("Invite link creation failed for request %s", request_id)
        note = (
            f"\n⚠️ Invite link nahi ban paya: {esc(exc)}\n"
            f"Admin zarurat par /resend {user_id} {key} use karein."
        )
        text += "Admin aapko link jaldi bhejenge."
    try:
        await context.bot.send_message(user_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except TelegramError as exc:
        note += f"\n⚠️ User ko DM nahi gaya: {esc(exc)}"
    return f"✅ Approved — {esc(fmt_dur(seconds))}, expiry {esc(fmt(expiry))}{note}"


async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Sirf admin ke liye.", show_alert=True)
        return
    await query.answer()
    parts = (query.data or "").split(":")
    try:
        action, request_id = parts[0], int(parts[1])
    except (ValueError, IndexError):
        await query.message.reply_text("Invalid request.")
        return

    request = await db.requests.find_one({"_id": request_id})
    if not request or request.get("status") != "pending":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        await query.message.reply_text(f"Request #{request_id} pehle hi handle ho chuki hai.")
        return

    if action == "ap":
        summary = await finalize(context, request_id, int(request["plan_secs"]))
        await query.edit_message_caption(
            caption=(query.message.caption_html or "") + "\n\n" + summary,
            parse_mode=ParseMode.HTML,
        )
    elif action == "chg":
        await query.edit_message_reply_markup(reply_markup=duration_keyboard(request_id))
    elif action == "cd":
        context.user_data["await_days"] = request_id
        await query.message.reply_text(
            f"Request #{request_id}: duration days mein bhejein (1-3650)."
        )
    elif action == "dur":
        if len(parts) != 3:
            return
        try:
            seconds = int(parts[2])
        except ValueError:
            return
        summary = await finalize(context, request_id, seconds)
        await query.edit_message_caption(
            caption=(query.message.caption_html or "") + "\n\n" + summary,
            parse_mode=ParseMode.HTML,
        )
    elif action == "rj":
        result = await db.requests.update_one(
            {"_id": request_id, "status": "pending"},
            {"$set": {"status": "rejected", "rejected_at": int(time.time())}},
        )
        if result
