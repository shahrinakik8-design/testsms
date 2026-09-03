#!/usr/bin/env python3
"""
Zebra SMS Telegram Bot — bottom keyboard UI + admin service/country manager.

Setup:
    pip install -r requirements.txt
    export TELEGRAM_BOT_TOKEN="123456:ABC-your-bot-father-token"
    export ADMIN_IDS="1586853120"   # your numeric Telegram user id(s)
    python3 bot.py

Find your Telegram user id via @userinfobot.

Admin can define "services" (e.g. Facebook, Instagram) and, under each
service, map country names to a range code (e.g. "Ivory Coast" -> "22501XXX").
Users then pick a service, then a country, and the bot allocates a number
from the mapped range automatically.

NOTE ON PERSISTENCE: services.json is written next to this script. On
platforms without a persistent disk (e.g. Railway without a volume), this
file resets on every redeploy. Attach a volume mounted at this script's
directory if you need the service/country list to survive restarts.
"""

import os
import json
import time
import asyncio
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BASE_URL = "https://zebrasms.com/api/v1"
API_KEY = "6U3G3DDZ6GB"
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

HEADERS = {"MAuth": API_KEY, "Content-Type": "application/json"}

SERVICES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "services.json")

ACTIVE_TASKS = {}          # chat_id -> asyncio.Task waiting for a code
KNOWN_USERS = set()        # chat_ids that have started the bot (in-memory only)
NUMBERS_ALLOCATED = 0
ADMIN_STATE = {}           # admin chat_id -> ("action_name", extra_data)

# services = { "Facebook": { "Ivory Coast": "22501XXX", ... }, ... }
services = {}


def load_services():
    global services
    if os.path.exists(SERVICES_FILE):
        try:
            with open(SERVICES_FILE, "r", encoding="utf-8") as f:
                services = json.load(f)
        except (json.JSONDecodeError, OSError):
            services = {}
    else:
        services = {}


def save_services():
    try:
        with open(SERVICES_FILE, "w", encoding="utf-8") as f:
            json.dump(services, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"Could not save services.json: {e}")


# ── API helpers ──────────────────────────────────────────────────────────

def _call_sync(method, path, **kwargs):
    resp = requests.request(method, f"{BASE_URL}{path}", headers=HEADERS, timeout=15, **kwargs)
    data = resp.json()
    meta = data.get("meta", {})
    if meta.get("code") != 0:
        raise RuntimeError(meta.get("error") or f"API error {meta.get('code')}")
    return data.get("data")


async def api_call(method, path, **kwargs):
    return await asyncio.to_thread(_call_sync, method, path, **kwargs)


def is_admin(user_id):
    return user_id in ADMIN_IDS


# ── Bottom (persistent) keyboard ────────────────────────────────────────

USER_ROWS = [["📱 Get Number", "📡 Browse Ranges"], ["📨 My Codes", "ℹ️ Help"]]


def main_kb(user_id):
    rows = [row[:] for row in USER_ROWS]
    if is_admin(user_id):
        rows.append(["🛠 Admin"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def back_inline(target="noop"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=target)]])


# ── /start ───────────────────────────────────────────────────────────────

WELCOME = (
    "👋 *Zebra SMS Bot*\n\n"
    "Grab a virtual number and receive its verification code, right here.\n\n"
    "Use the buttons below 👇"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    KNOWN_USERS.add(update.effective_chat.id)
    await update.message.reply_text(
        WELCOME, parse_mode=ParseMode.MARKDOWN, reply_markup=main_kb(update.effective_user.id)
    )


# ── Get Number by service + country ─────────────────────────────────────

async def show_services(update_or_query, edit=False):
    if not services:
        text = "No services configured yet. Ask an admin to add one, or use `/getnum <range>` directly."
        if edit:
            await update_or_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
        else:
            await update_or_query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return

    buttons = [[InlineKeyboardButton(name, callback_data=f"svc:{name}")] for name in services.keys()]
    text = "📱 *Choose a service:*"
    if edit:
        await update_or_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update_or_query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))


async def show_countries(query, service):
    countries = services.get(service, {})
    if not countries:
        await query.edit_message_text(f"No countries configured for {service} yet.", reply_markup=back_inline())
        return
    buttons = [
        [InlineKeyboardButton(country, callback_data=f"svccountry:{service}:{country}")]
        for country in countries.keys()
    ]
    await query.edit_message_text(
        f"🌍 *{service}* — choose a country:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def service_callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id
    KNOWN_USERS.add(chat_id)

    if data.startswith("svc:"):
        service = data.split(":", 1)[1]
        await show_countries(query, service)

    elif data.startswith("svccountry:"):
        _, service, country = data.split(":", 2)
        rng = services.get(service, {}).get(country)
        if not rng:
            await query.edit_message_text("That option no longer exists.", reply_markup=back_inline())
            return
        await start_getnum_flow(chat_id, rng, context, edit_query=query, label=f"{service} / {country}")


# ── Raw range browsing (unchanged from before, kept as a fallback) ──────

async def show_sender_list(update_or_query, edit=False):
    try:
        data = await api_call("GET", "/publicapi/liveaccess")
    except RuntimeError as e:
        text = f"⚠️ Error: {e}"
        if edit:
            await update_or_query.edit_message_text(text)
        else:
            await update_or_query.message.reply_text(text)
        return

    rows = data.get("rows", [])
    if not rows:
        text = "No senders delivering right now."
        if edit:
            await update_or_query.edit_message_text(text)
        else:
            await update_or_query.message.reply_text(text)
        return

    buttons = [[InlineKeyboardButton(f"{r['sender']} ({len(r['ranges'])})", callback_data=f"sender:{r['sender']}")]
               for r in rows[:30]]
    kb = InlineKeyboardMarkup(buttons)
    text = "📡 *Senders delivering right now*\nTap one to see its raw ranges:"
    if edit:
        await update_or_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await update_or_query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def show_ranges_for_sender(query, sender):
    try:
        data = await api_call("GET", "/publicapi/liveaccess", params={"sender": sender})
    except RuntimeError as e:
        await query.edit_message_text(f"⚠️ Error: {e}", reply_markup=back_inline())
        return
    rows = data.get("rows", [])
    ranges = rows[0]["ranges"] if rows else []
    if not ranges:
        await query.edit_message_text(f"No ranges found for {sender}.", reply_markup=back_inline())
        return
    buttons = [[InlineKeyboardButton(rng, callback_data=f"getnum:{rng}")] for rng in ranges]
    await query.edit_message_text(
        f"📡 *{sender}* — tap a range to allocate a number:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def raw_callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id
    KNOWN_USERS.add(chat_id)

    if data.startswith("sender:"):
        await show_ranges_for_sender(query, data.split(":", 1)[1])
    elif data.startswith("getnum:"):
        rng = data.split(":", 1)[1]
        await start_getnum_flow(chat_id, rng, context, edit_query=query)


# ── Get number + wait for code (core logic, reused everywhere) ─────────

async def getnum_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    KNOWN_USERS.add(chat_id)
    if not context.args:
        await update.message.reply_text("Usage: /getnum 22501XXX")
        return
    await start_getnum_flow(chat_id, context.args[0], context)


async def start_getnum_flow(chat_id, rng, context, edit_query=None, label=None):
    global NUMBERS_ALLOCATED

    existing = ACTIVE_TASKS.get(chat_id)
    if existing and not existing.done():
        msg = "⏳ Already waiting on a number — send /cancel first."
        if edit_query:
            await edit_query.edit_message_text(msg)
        else:
            await context.bot.send_message(chat_id, msg)
        return

    try:
        data = await api_call("POST", "/publicapi/getnum", json={"range": rng})
    except RuntimeError as e:
        msg = f"⚠️ Error: {e}"
        if edit_query:
            await edit_query.edit_message_text(msg)
        else:
            await context.bot.send_message(chat_id, msg)
        return

    if not data or not data.get("rows"):
        msg = "⚠️ No number returned for that range."
        if edit_query:
            await edit_query.edit_message_text(msg)
        else:
            await context.bot.send_message(chat_id, msg)
        return

    row = data["rows"][0]
    number = row["number"]
    expires_ms = row["expires_ms"]
    expires_str = time.strftime("%H:%M:%S", time.localtime(expires_ms / 1000))
    NUMBERS_ALLOCATED += 1

    header = f"🏷 {label}\n" if label else ""
    text = (
        f"{header}✅ *Allocated:* `{number}`\n"
        f"🌍 {row['country']} / {row['operator']}\n"
        f"⏰ Expires at {expires_str}\n\n"
        f"⌛ Waiting for the code... (/cancel to stop)"
    )
    if edit_query:
        await edit_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
    else:
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)

    task = asyncio.create_task(_wait_for_code(chat_id, number, expires_ms, context))
    ACTIVE_TASKS[chat_id] = task


async def _wait_for_code(chat_id, number, expires_ms, context):
    try:
        seen_data = await api_call("GET", "/publicapi/getupdate")
        seen = {(r["number"], r["message"], r["at_ms"]) for r in seen_data.get("rows", [])}
    except RuntimeError:
        seen = set()

    try:
        while True:
            if int(time.time() * 1000) > expires_ms:
                await context.bot.send_message(
                    chat_id, f"⌛ `{number}` expired with no code received.", parse_mode=ParseMode.MARKDOWN
                )
                return
            try:
                data = await api_call("GET", "/publicapi/getupdate")
            except RuntimeError as e:
                await context.bot.send_message(chat_id, f"⚠️ Error polling for code: {e}")
                return

            for r in data.get("rows", []):
                key = (r["number"], r["message"], r["at_ms"])
                if r["number"] == number and key not in seen:
                    await context.bot.send_message(
                        chat_id,
                        f"📨 *Code from {r['sender']}* on `{number}`:\n`{r['message']}`",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return
            await asyncio.sleep(3)
    except asyncio.CancelledError:
        return
    finally:
        ACTIVE_TASKS.pop(chat_id, None)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    task = ACTIVE_TASKS.get(chat_id)
    if task and not task.done():
        task.cancel()
        await update.message.reply_text("🛑 Stopped waiting.")
    else:
        await update.message.reply_text("Nothing in progress.")


# ── Codes list ───────────────────────────────────────────────────────────

async def codes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    KNOWN_USERS.add(update.effective_chat.id)
    try:
        data = await api_call("GET", "/publicapi/getupdate")
    except RuntimeError as e:
        await update.message.reply_text(f"⚠️ Error: {e}")
        return
    rows = data.get("rows", [])[:15]
    if not rows:
        await update.message.reply_text("No codes received yet.")
        return
    lines = [f"{r['number']} ({r['sender']}): {r['message']}" for r in rows]
    await update.message.reply_text("\n".join(lines))


HELP_TEXT = (
    "ℹ️ *How it works*\n\n"
    "📱 *Get Number* — pick a service, then a country; the bot allocates a "
    "number and waits for its code automatically\n"
    "📡 *Browse Ranges* — see raw ranges by sender id (advanced/manual)\n"
    "📨 *My Codes* — your last received codes\n\n"
    "You can also type `/getnum 22501XXX` directly."
)


# ── Admin panel ──────────────────────────────────────────────────────────

def admin_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data="adm:stats")],
        [InlineKeyboardButton("🧩 Manage Services", callback_data="adm:services")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="adm:broadcast")],
        [InlineKeyboardButton("⬅️ Close", callback_data="adm:close")],
    ])


def services_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Service", callback_data="adm:svc_add")],
        [InlineKeyboardButton("🌍 Add Country to Service", callback_data="adm:svc_addcountry")],
        [InlineKeyboardButton("📋 List Services", callback_data="adm:svc_list")],
        [InlineKeyboardButton("🗑 Remove Service", callback_data="adm:svc_remove")],
        [InlineKeyboardButton("⬅️ Back", callback_data="adm:home")],
    ])


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorized.")
        return
    await update.message.reply_text("🛠 *Admin Panel*", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu_kb())


async def admin_callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data == "adm:home":
        ADMIN_STATE.pop(chat_id, None)
        await query.edit_message_text("🛠 *Admin Panel*", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu_kb())

    elif data == "adm:close":
        ADMIN_STATE.pop(chat_id, None)
        await query.edit_message_text("Admin panel closed.")

    elif data == "adm:stats":
        text = (
            "📊 *Stats*\n\n"
            f"👥 Known users: {len(KNOWN_USERS)}\n"
            f"⌛ Active waits: {len(ACTIVE_TASKS)}\n"
            f"📱 Numbers allocated this run: {NUMBERS_ALLOCATED}\n"
            f"🧩 Services configured: {len(services)}\n\n"
            "_Counts reset if the bot restarts (in-memory only)._"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu_kb())

    elif data == "adm:broadcast":
        ADMIN_STATE[chat_id] = ("broadcast", None)
        await query.edit_message_text("📢 Send the message to broadcast to all known users.\nSend /cancel to abort.")

    elif data == "adm:services":
        await query.edit_message_text("🧩 *Manage Services*", parse_mode=ParseMode.MARKDOWN, reply_markup=services_menu_kb())

    elif data == "adm:svc_add":
        ADMIN_STATE[chat_id] = ("add_service", None)
        await query.edit_message_text("➕ Send the new service name (e.g. Facebook).\nSend /cancel to abort.")

    elif data == "adm:svc_addcountry":
        if not services:
            await query.edit_message_text("No services yet — add one first.", reply_markup=services_menu_kb())
            return
        buttons = [[InlineKeyboardButton(name, callback_data=f"adm:svcpick:{name}")] for name in services.keys()]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm:services")])
        await query.edit_message_text("Pick the service to add a country to:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("adm:svcpick:"):
        service = data.split(":", 2)[2]
        ADMIN_STATE[chat_id] = ("add_country", service)
        await query.edit_message_text(
            f"🌍 Send the country and range for *{service}* like this:\n`Ivory Coast 22501XXX`\n\nSend /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "adm:svc_list":
        if not services:
            text = "No services configured yet."
        else:
            lines = []
            for name, countries in services.items():
                lines.append(f"*{name}*")
                if countries:
                    for country, rng in countries.items():
                        lines.append(f"  • {country} — `{rng}`")
                else:
                    lines.append("  _(no countries yet)_")
            text = "📋 *Services*\n\n" + "\n".join(lines)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=services_menu_kb())

    elif data == "adm:svc_remove":
        if not services:
            await query.edit_message_text("No services to remove.", reply_markup=services_menu_kb())
            return
        buttons = [[InlineKeyboardButton(f"🗑 {name}", callback_data=f"adm:svcdel:{name}")] for name in services.keys()]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm:services")])
        await query.edit_message_text("Pick a service to remove:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("adm:svcdel:"):
        service = data.split(":", 2)[2]
        services.pop(service, None)
        save_services()
        await query.edit_message_text(f"🗑 Removed *{service}*.", parse_mode=ParseMode.MARKDOWN, reply_markup=services_menu_kb())


async def admin_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the follow-up text for whatever admin flow is in progress."""
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_STATE:
        return False  # not handled here

    action, extra = ADMIN_STATE.pop(chat_id)
    text = update.message.text.strip()

    if action == "broadcast":
        sent, failed = 0, 0
        for uid in list(KNOWN_USERS):
            try:
                await context.bot.send_message(uid, f"📢 {text}")
                sent += 1
            except Exception:
                failed += 1
        await update.message.reply_text(f"✅ Broadcast sent to {sent} user(s). Failed: {failed}.")

    elif action == "add_service":
        if text in services:
            await update.message.reply_text(f"'{text}' already exists.")
        else:
            services[text] = {}
            save_services()
            await update.message.reply_text(f"✅ Service '{text}' added. Now add countries to it from the admin panel.")

    elif action == "add_country":
        service = extra
        if service not in services:
            await update.message.reply_text("That service no longer exists.")
            return True
        if " " not in text:
            await update.message.reply_text("Format: `Country Name RANGECODE` — try again from the admin panel.", parse_mode=ParseMode.MARKDOWN)
            return True
        country, rng = text.rsplit(" ", 1)
        services[service][country.strip()] = rng.strip()
        save_services()
        await update.message.reply_text(f"✅ Added {country.strip()} → `{rng.strip()}` under {service}.", parse_mode=ParseMode.MARKDOWN)

    return True


# ── Bottom-keyboard text router ─────────────────────────────────────────

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    KNOWN_USERS.add(chat_id)

    # Admin multi-step flows take priority over button labels
    if await admin_text_handler(update, context):
        return

    text = update.message.text.strip()

    if text == "📱 Get Number":
        await show_services(update)
    elif text == "📡 Browse Ranges":
        await show_sender_list(update)
    elif text == "📨 My Codes":
        await codes_command(update, context)
    elif text == "ℹ️ Help":
        await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)
    elif text == "🛠 Admin":
        await admin_command(update, context)
    # else: not a recognized button — ignore silently


# ── Wiring ───────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN first.")
    if not ADMIN_IDS:
        print("⚠️  No ADMIN_IDS set — the Admin button/menu will be unusable until you set that env var.")

    load_services()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("getnum", getnum_command))
    app.add_handler(CommandHandler("codes", codes_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("admin", admin_command))

    app.add_handler(CallbackQueryHandler(admin_callback_router, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(service_callback_router, pattern=r"^svc"))
    app.add_handler(CallbackQueryHandler(raw_callback_router, pattern=r"^(sender:|getnum:)"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
