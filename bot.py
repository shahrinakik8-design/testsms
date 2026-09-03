#!/usr/bin/env python3
"""
Zebra SMS Telegram Bot — with inline buttons + admin panel.

Setup:
    pip install -r requirements.txt
    export TELEGRAM_BOT_TOKEN="123456:ABC-your-bot-father-token"
    export ADMIN_IDS="111111111,222222222"   # your Telegram numeric user id(s), comma-separated
    python3 bot.py

To find your own Telegram user id: message @userinfobot on Telegram.
"""

import os
import time
import asyncio
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

ACTIVE_TASKS = {}          # chat_id -> asyncio.Task waiting for a code
KNOWN_USERS = set()        # chat_ids that have started the bot (in-memory only)
NUMBERS_ALLOCATED = 0      # simple running counter for the admin stats view
AWAITING_BROADCAST = set() # admin chat_ids currently composing a broadcast


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


# ── Keyboards ────────────────────────────────────────────────────────────

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 Live Ranges", callback_data="menu:ranges")],
        [InlineKeyboardButton("📱 Get Number", callback_data="menu:getnum")],
        [InlineKeyboardButton("📨 My Codes", callback_data="menu:codes")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="menu:help")],
    ])


def back_kb(target="menu:home"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=target)]])


def admin_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data="admin:stats")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin:broadcast")],
        [InlineKeyboardButton("⬅️ Close", callback_data="admin:close")],
    ])


# ── /start & main menu ──────────────────────────────────────────────────

WELCOME = (
    "👋 *Zebra SMS Bot*\n\n"
    "Grab a virtual number and receive its verification code, right here.\n\n"
    "Choose an option below 👇"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    KNOWN_USERS.add(update.effective_chat.id)
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    KNOWN_USERS.add(chat_id)
    data = query.data

    if data == "menu:home":
        await query.edit_message_text(WELCOME, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())

    elif data == "menu:help":
        text = (
            "ℹ️ *How it works*\n\n"
            "📡 *Live Ranges* — see which sender ids are delivering codes right now\n"
            "📱 *Get Number* — allocate a number and the bot waits for its code automatically\n"
            "📨 *My Codes* — your last received codes\n\n"
            "You can also just type `/getnum 22501XXX` directly."
        )
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())

    elif data == "menu:codes":
        await show_codes(query)

    elif data == "menu:ranges":
        await show_sender_list(query)

    elif data == "menu:getnum":
        await query.edit_message_text(
            "📱 Send the range you want a number from, e.g.\n`/getnum 22501XXX`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )

    elif data.startswith("sender:"):
        await show_ranges_for_sender(query, data.split(":", 1)[1])

    elif data.startswith("getnum:"):
        await do_getnum(query, context, data.split(":", 1)[1])


# ── Live ranges browsing ────────────────────────────────────────────────

async def show_sender_list(query):
    try:
        data = await api_call("GET", "/publicapi/liveaccess")
    except RuntimeError as e:
        await query.edit_message_text(f"⚠️ Error: {e}", reply_markup=back_kb())
        return

    rows = data.get("rows", [])
    if not rows:
        await query.edit_message_text("No senders delivering right now.", reply_markup=back_kb())
        return

    buttons = [[InlineKeyboardButton(f"{r['sender']} ({len(r['ranges'])})", callback_data=f"sender:{r['sender']}")]
               for r in rows[:30]]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:home")])
    await query.edit_message_text(
        "📡 *Senders delivering right now*\nTap one to see its ranges:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_ranges_for_sender(query, sender):
    try:
        data = await api_call("GET", "/publicapi/liveaccess", params={"sender": sender})
    except RuntimeError as e:
        await query.edit_message_text(f"⚠️ Error: {e}", reply_markup=back_kb())
        return

    rows = data.get("rows", [])
    ranges = rows[0]["ranges"] if rows else []
    if not ranges:
        await query.edit_message_text(f"No ranges found for {sender}.", reply_markup=back_kb("menu:ranges"))
        return

    buttons = [[InlineKeyboardButton(rng, callback_data=f"getnum:{rng}")] for rng in ranges]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:ranges")])
    await query.edit_message_text(
        f"📡 *{sender}* — tap a range to allocate a number:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── Get number + wait for code ──────────────────────────────────────────

async def do_getnum(query, context, rng):
    chat_id = query.message.chat_id
    await start_getnum_flow(chat_id, rng, context, edit_query=query)


async def getnum_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    KNOWN_USERS.add(chat_id)
    if not context.args:
        await update.message.reply_text("Usage: /getnum 22501XXX")
        return
    await start_getnum_flow(chat_id, context.args[0], context)


async def start_getnum_flow(chat_id, rng, context, edit_query=None):
    global NUMBERS_ALLOCATED

    existing = ACTIVE_TASKS.get(chat_id)
    if existing and not existing.done():
        msg = "⏳ Already waiting on a number — send /cancel first."
        if edit_query:
            await edit_query.edit_message_text(msg, reply_markup=back_kb())
        else:
            await context.bot.send_message(chat_id, msg)
        return

    try:
        data = await api_call("POST", "/publicapi/getnum", json={"range": rng})
    except RuntimeError as e:
        msg = f"⚠️ Error: {e}"
        if edit_query:
            await edit_query.edit_message_text(msg, reply_markup=back_kb())
        else:
            await context.bot.send_message(chat_id, msg)
        return

    if not data or not data.get("rows"):
        msg = "⚠️ No number returned for that range."
        if edit_query:
            await edit_query.edit_message_text(msg, reply_markup=back_kb())
        else:
            await context.bot.send_message(chat_id, msg)
        return

    row = data["rows"][0]
    number = row["number"]
    expires_ms = row["expires_ms"]
    expires_str = time.strftime("%H:%M:%S", time.localtime(expires_ms / 1000))
    NUMBERS_ALLOCATED += 1

    text = (
        f"✅ *Allocated:* `{number}`\n"
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

async def show_codes(query):
    try:
        data = await api_call("GET", "/publicapi/getupdate")
    except RuntimeError as e:
        await query.edit_message_text(f"⚠️ Error: {e}", reply_markup=back_kb())
        return

    rows = data.get("rows", [])[:15]
    if not rows:
        await query.edit_message_text("No codes received yet.", reply_markup=back_kb())
        return

    lines = [f"`{r['number']}` ({r['sender']}): {r['message']}" for r in rows]
    await query.edit_message_text(
        "📨 *Last codes*\n\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb()
    )


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


# ── Admin panel ──────────────────────────────────────────────────────────

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorized.")
        return
    await update.message.reply_text("🛠 *Admin Panel*", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu_kb())


async def admin_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data == "admin:stats":
        text = (
            "📊 *Stats*\n\n"
            f"👥 Known users: {len(KNOWN_USERS)}\n"
            f"⌛ Active waits: {len(ACTIVE_TASKS)}\n"
            f"📱 Numbers allocated this run: {NUMBERS_ALLOCATED}\n\n"
            "_Counts reset if the bot restarts (in-memory only)._"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu_kb())

    elif data == "admin:broadcast":
        AWAITING_BROADCAST.add(chat_id)
        await query.edit_message_text(
            "📢 Send the message you want to broadcast to all known users.\nSend /cancel to abort.",
        )

    elif data == "admin:close":
        await query.edit_message_text("Admin panel closed.")


async def admin_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches the next message from an admin who tapped Broadcast."""
    chat_id = update.effective_chat.id
    if chat_id not in AWAITING_BROADCAST:
        return
    AWAITING_BROADCAST.discard(chat_id)

    text = update.message.text
    sent, failed = 0, 0
    for uid in list(KNOWN_USERS):
        try:
            await context.bot.send_message(uid, f"📢 {text}")
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"✅ Broadcast sent to {sent} user(s). Failed: {failed}.")


# ── Wiring ───────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN first.")
    if not ADMIN_IDS:
        print("⚠️  No ADMIN_IDS set — /admin will be unusable until you set that env var.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("getnum", getnum_command))
    app.add_handler(CommandHandler("codes", codes_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("admin", admin_command))

    app.add_handler(CallbackQueryHandler(admin_router, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(menu_router))  # catches menu:*, sender:*, getnum:*

    # Admin broadcast text capture — must not swallow normal /commands
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text_handler))

    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
