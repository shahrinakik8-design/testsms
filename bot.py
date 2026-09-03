#!/usr/bin/env python3
"""
Zebra SMS Telegram Bot (multi-user)
Wraps the getnum / getupdate / liveaccess endpoints as Telegram commands.
Each /getnum wait runs as its own background asyncio task, so many users
can use the bot at the same time without blocking each other.

Setup:
    pip install -r requirements.txt
    export TELEGRAM_BOT_TOKEN="123456:ABC-your-bot-father-token"
    python3 telegram_bot.py

Commands:
    /ranges [sender]   - show which ranges are delivering right now
    /getnum <range>    - allocate a number from that range and wait for its code
    /codes             - show your last received codes
    /cancel            - stop waiting for your current number's code
"""

import os
import time
import asyncio
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BASE_URL = "https://zebrasms.com/api/v1"
API_KEY = "6U3G3DDZ6GB"   # from the Zebra SMS dashboard
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

HEADERS = {
    "MAuth": API_KEY,
    "Content-Type": "application/json",
}

# chat_id -> asyncio.Task currently waiting for that chat's code
ACTIVE_TASKS = {}


def _call_sync(method, path, **kwargs):
    """Blocking HTTP call — always run this through asyncio.to_thread."""
    url = f"{BASE_URL}{path}"
    resp = requests.request(method, url, headers=HEADERS, timeout=15, **kwargs)
    data = resp.json()
    meta = data.get("meta", {})
    if meta.get("code") != 0:
        raise RuntimeError(meta.get("error") or f"API error {meta.get('code')}")
    return data.get("data")


async def api_call(method, path, **kwargs):
    """Non-blocking wrapper so one user's request never stalls the bot's event loop."""
    return await asyncio.to_thread(_call_sync, method, path, **kwargs)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Zebra SMS bot.\n\n"
        "/ranges [sender] - which ranges are delivering\n"
        "/getnum <range>  - allocate a number and wait for its code\n"
        "/codes           - last received codes\n"
        "/cancel          - stop waiting for your current code"
    )


async def ranges(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender = context.args[0] if context.args else None
    params = {"sender": sender} if sender else {}
    try:
        data = await api_call("GET", "/publicapi/liveaccess", params=params)
    except RuntimeError as e:
        await update.message.reply_text(f"Error: {e}")
        return

    rows = data.get("rows", [])
    if not rows:
        await update.message.reply_text("No matching senders right now.")
        return

    lines = [f"*{r['sender']}* -> {', '.join(r['ranges'])}" for r in rows]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def codes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = await api_call("GET", "/publicapi/getupdate")
    except RuntimeError as e:
        await update.message.reply_text(f"Error: {e}")
        return

    rows = data.get("rows", [])
    if not rows:
        await update.message.reply_text("No codes received yet.")
        return

    lines = [f"{r['number']} ({r['sender']}): {r['message']}" for r in rows[:15]]
    await update.message.reply_text("\n".join(lines))


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    task = ACTIVE_TASKS.get(chat_id)
    if task and not task.done():
        task.cancel()
        await update.message.reply_text("Stopped waiting.")
    else:
        await update.message.reply_text("Nothing in progress.")


async def _wait_for_code(chat_id, number, expires_ms, context: ContextTypes.DEFAULT_TYPE):
    """Runs as its own background task per chat — doesn't block other users."""
    try:
        seen_data = await api_call("GET", "/publicapi/getupdate")
        seen = {(r["number"], r["message"], r["at_ms"]) for r in seen_data.get("rows", [])}
    except RuntimeError:
        seen = set()

    try:
        while True:
            if int(time.time() * 1000) > expires_ms:
                await context.bot.send_message(chat_id, f"{number} expired with no code received.")
                return

            try:
                data = await api_call("GET", "/publicapi/getupdate")
            except RuntimeError as e:
                await context.bot.send_message(chat_id, f"Error polling for code: {e}")
                return

            for r in data.get("rows", []):
                key = (r["number"], r["message"], r["at_ms"])
                if r["number"] == number and key not in seen:
                    await context.bot.send_message(
                        chat_id,
                        f"Code from {r['sender']} on {number}:\n{r['message']}",
                    )
                    return

            await asyncio.sleep(3)
    except asyncio.CancelledError:
        # /cancel was called — exit quietly.
        return
    finally:
        ACTIVE_TASKS.pop(chat_id, None)


async def getnum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text("Usage: /getnum 22501XXX")
        return
    rng = context.args[0]

    existing = ACTIVE_TASKS.get(chat_id)
    if existing and not existing.done():
        await update.message.reply_text("Already waiting on a number — use /cancel first.")
        return

    try:
        data = await api_call("POST", "/publicapi/getnum", json={"range": rng})
    except RuntimeError as e:
        await update.message.reply_text(f"Error: {e}")
        return

    if not data or not data.get("rows"):
        await update.message.reply_text("No number returned for that range.")
        return

    row = data["rows"][0]
    number = row["number"]
    expires_ms = row["expires_ms"]
    expires_str = time.strftime("%H:%M:%S", time.localtime(expires_ms / 1000))

    await update.message.reply_text(
        f"Allocated: {number}\n"
        f"{row['country']} / {row['operator']}\n"
        f"Expires at {expires_str}\n\n"
        f"Waiting for the code... (/cancel to stop)"
    )

    task = asyncio.create_task(_wait_for_code(chat_id, number, expires_ms, context))
    ACTIVE_TASKS[chat_id] = task


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Set TELEGRAM_BOT_TOKEN first, e.g.\n"
            "  export TELEGRAM_BOT_TOKEN='123456:ABC-your-token'"
        )

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ranges", ranges))
    app.add_handler(CommandHandler("getnum", getnum))
    app.add_handler(CommandHandler("codes", codes))
    app.add_handler(CommandHandler("cancel", cancel))

    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
