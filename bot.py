import os
import re
import json
import time
import math
import asyncio
import logging
import datetime
import html
import requests
import aiohttp
from py_liveaccess import LiveAccess
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==================== LOGGING ====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8685478236:AAEbWdFRZL0xy5qq1FJDzD3TO9trUXMguWQ")
API_KEY = os.getenv("ZEBRA_API_KEY", "6U3G3DDZ6GB")
BASE_URL = "https://zebrasms.com/stubs/handler_api.php"

# Telegram Admin & Channel Config
ADMIN_ID = 6363063852
OTP_GROUP_ID = -1001234567890  # Corrected standard supergroup ID format (-100 + 10 digits)

# File Paths
DATA_FILE = "users.json"
MONITOR_FILE = "monitored_numbers.json"
PRICES_FILE = "service_prices.json"
HISTORY_FILE = "otp_history.json"
LEADERBOARD_FILE = "leaderboard.json"

# Lock for Thread Safety
DATA_LOCK = asyncio.Lock()

# Global Caches
LIVEACCESS_API_KEY = "liveaccess-bd-v1"
liveaccess_services_cache = []

# ==================== DATA STORAGE HELPERS ====================
def load_json(filename, default):
    if not os.path.exists(filename):
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=4)
        return default
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading {filename}: {e}")
        return default

def save_json(filename, data):
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving {filename}: {e}")

# Load Initial Storage
users = load_json(DATA_FILE, {})
monitored_numbers = load_json(MONITOR_FILE, [])
service_prices = load_json(PRICES_FILE, {})
otp_history = load_json(HISTORY_FILE, [])
leaderboard = load_json(LEADERBOARD_FILE, {})

def get_user(uid):
    uid_str = str(uid)
    if uid_str not in users:
        users[uid_str] = {
            "balance": 0.0,
            "lang": "en",
            "stats": {
                "today_date": str(datetime.date.today()),
                "today_numbers": 0,
                "today_otps": 0,
                "total_numbers": 0,
                "total_otps": 0
            }
        }
        save_json(DATA_FILE, users)
    return users[uid_str]

def update_user_balance(uid, amount):
    uid_str = str(uid)
    u = get_user(uid)
    u["balance"] = round(u.get("balance", 0.0) + amount, 2)
    save_json(DATA_FILE, users)

def get_user_lang(uid):
    return get_user(uid).get("lang", "en")

def set_user_lang(uid, lang):
    u = get_user(uid)
    u["lang"] = lang
    save_json(DATA_FILE, users)

def get_user_stats(uid):
    u = get_user(uid)
    today_str = str(datetime.date.today())
    stats = u.setdefault("stats", {
        "today_date": today_str,
        "today_numbers": 0,
        "today_otps": 0,
        "total_numbers": 0,
        "total_otps": 0
    })
    if stats.get("today_date") != today_str:
        stats["today_date"] = today_str
        stats["today_numbers"] = 0
        stats["today_otps"] = 0
        save_json(DATA_FILE, users)
    return stats

def increment_stat(uid, stat_key):
    stats = get_user_stats(uid)
    stats[f"today_{stat_key}"] += 1
    stats[f"total_{stat_key}"] += 1
    save_json(DATA_FILE, users)

def format_balance(val):
    return f"{float(val):.2f}"

# ==================== ASYNC ZEBRA & LIVEACCESS API ====================
async def zebra_get_number(service_code):
    url = f"{BASE_URL}?api_key={API_KEY}&action=getNumber&service={service_code}&country=22"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=15) as resp:
                text = await resp.text()
                if "ACCESS_NUMBER" in text:
                    parts = text.split(":")
                    return True, {"id": parts[1], "number": parts[2]}
                return False, text
        except Exception as e:
            return False, str(e)

async def zebra_get_status(activation_id):
    url = f"{BASE_URL}?api_key={API_KEY}&action=getStatus&id={activation_id}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=15) as resp:
                return await resp.text()
        except Exception as e:
            return f"ERROR:{e}"

async def zebra_set_status(activation_id, status_code):
    url = f"{BASE_URL}?api_key={API_KEY}&action=setStatus&id={activation_id}&status={status_code}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=15) as resp:
                return await resp.text()
        except Exception as e:
            return f"ERROR:{e}"

async def liveaccess_refresh_loop():
    global liveaccess_services_cache
    while True:
        try:
            la = LiveAccess(LIVEACCESS_API_KEY)
            services = await asyncio.to_thread(la.get_services)
            if services:
                liveaccess_services_cache = services
        except Exception as e:
            logger.error(f"LiveAccess refresh failed: {e}")
        await asyncio.sleep(300)

# ==================== LANGUAGE & KEYBOARD SETUP ====================
LANG_TEXTS = {
    "en": {
        "welcome": "👋 <b>Welcome to OTP Bot!</b>\nSelect an option from the menu below:",
        "profile": "👤 Profile",
        "buy": "📱 Buy Number",
        "lang_select": "🌐 Change Language",
        "topup": "💳 Top Up",
        "support": "💬 Support",
        "admin": "⚙️ Admin Menu",
        "lang_changed": "✅ Language changed to English!",
        "no_bal": "❌ Insufficient balance! Please top up your wallet."
    },
    "bn": {
        "welcome": "👋 <b>ওটিপি বোটে স্বাগতম!</b>\nনিচের মেনু থেকে একটি অপশন নির্বাচন করুন:",
        "profile": "👤 প্রোফাইল",
        "buy": "📱 নম্বর কিনুন",
        "lang_select": "🌐 ভাষা পরিবর্তন",
        "topup": "💳 টপ আপ",
        "support": "💬 সাপোর্ট",
        "admin": "⚙️ এডমিন মেনু",
        "lang_changed": "✅ ভাষা সফলভাবে বাংলায় পরিবর্তিত হয়েছে!",
        "no_bal": "❌ অপর্যাপ্ত ব্যালেন্স! দয়া করে আপনার ওয়ালেট টপআপ করুন।"
    }
}

T_PROFILE = ["👤 Profile", "👤 প্রোফাইল"]
T_BUY = ["📱 Buy Number", "📱 নম্বর কিনুন"]
T_LANG = ["🌐 Change Language", "🌐 ভাষা পরিবর্তন"]
T_TOPUP = ["💳 Top Up", "💳 টপ আপ"]
T_SUPPORT = ["💬 Support", "💬 সাপোর্ট"]
T_ADMIN = ["⚙️ Admin Menu", "⚙️ এডমিন মেনু"]

def main_keyboard(uid):
    lang = get_user_lang(uid)
    t = LANG_TEXTS[lang]
    keyboard = [
        [KeyboardButton(t["buy"]), KeyboardButton(t["profile"])],
        [KeyboardButton(t["topup"]), KeyboardButton(t["lang_select"])],
        [KeyboardButton(t["support"])]
    ]
    if int(uid) == ADMIN_ID:
        keyboard.append([KeyboardButton(t["admin"])])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ==================== QUEUE WORKER & MONITOR ====================
queue = asyncio.Queue()

async def worker():
    while True:
        task = await queue.get()
        try:
            await task()
        except Exception as e:
            logger.error(f"Task processing error: {e}")
        finally:
            queue.task_done()

async def monitor_loop(app):
    while True:
        try:
            async with DATA_LOCK:
                to_remove = []
                for item in list(monitored_numbers):
                    activation_id = item["id"]
                    uid = item["uid"]
                    res = await zebra_get_status(activation_id)

                    if "STATUS_OK" in res:
                        otp = res.split(":")[1]
                        to_remove.append(item)
                        increment_stat(uid, "otps")

                        msg = (
                            f"🔑 <b>OTP Received!</b>\n\n"
                            f"📱 Number: <code>{item['number']}</code>\n"
                            f"💬 Code: <code>{otp}</code>"
                        )
                        try:
                            await app.bot.send_message(chat_id=uid, text=msg, parse_mode="HTML")
                            await app.bot.send_message(
                                chat_id=OTP_GROUP_ID,
                                text=f"🎉 <b>New OTP!</b>\nNumber: <code>{item['number']}</code>\nCode: <code>{otp}</code>",
                                parse_mode="HTML"
                            )
                        except Exception as e:
                            logger.error(f"Send OTP error: {e}")

                    elif "STATUS_CANCEL" in res:
                        to_remove.append(item)

                for r in to_remove:
                    if r in monitored_numbers:
                        monitored_numbers.remove(r)
                if to_remove:
                    save_json(MONITOR_FILE, monitored_numbers)
        except Exception as e:
            logger.error(f"Monitor loop error: {e}")
        await asyncio.sleep(5)

# ==================== COMMAND & MESSAGE HANDLERS ====================
async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top_users = sorted(users.items(), key=lambda x: x[1].get("stats", {}).get("total_otps", 0), reverse=True)[:10]
    text = "🏆 <b>TOP OTP RECEIVERS</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
    for idx, (u_id, u_data) in enumerate(top_users, 1):
        otps = u_data.get("stats", {}).get("total_otps", 0)
        text += f"{idx}. ID: <code>{u_id}</code> - {otps} OTPs\n"
    await update.message.reply_text(text, parse_mode="HTML")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    uid = update.effective_user.id
    lang = get_user_lang(uid)

    # Language Switcher Trigger
    if text in T_LANG:
        new_lang = "bn" if lang == "en" else "en"
        set_user_lang(uid, new_lang)
        await update.message.reply_text(
            LANG_TEXTS[new_lang]["lang_changed"],
            reply_markup=main_keyboard(uid)
        )
        return

    # User Profile Section (Fully Repaired Format)
    if text in T_PROFILE:
        user_data = get_user(uid)
        stats = get_user_stats(uid)
        user = update.effective_user
        full_name = html.escape(user.full_name)
        username = html.escape(user.username or "N/A")

        if lang == "bn":
            profile_text = (
                f"👤 <b>ইউজার প্রোফাইল</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🏷️ <b>নাম:</b> <code>{full_name}</code>\n"
                f"🆔 <b>ইউজারনেম:</b> @{username}\n"
                f"🗝️ <b>ইউজার আইডি:</b> <code>{uid}</code>\n\n"
                f"💵 <b>ওয়ালেট ব্যালেন্স:</b> <code>{format_balance(user_data.get('balance', 0))} BDT</code>\n\n"
                f"📊 <b>আজকের স্ট্যাটাস:</b>\n"
                f"<blockquote>📱 নম্বর নিয়েছেন: <code>{stats['today_numbers']}</code>\n"
                f"🔑 ওটিপি পেয়েছেন: <code>{stats['today_otps']}</code></blockquote>\n\n"
                f"🌐 <b>সর্বমোট স্ট্যাটাস:</b>\n"
                f"<blockquote>📱 নম্বর নিয়েছেন: <code>{stats['total_numbers']}</code>\n"
                f"🔑 ওটিপি পেয়েছেন: <code>{stats['total_otps']}</code></blockquote>"
            )
        else:
            profile_text = (
                f"👤 <b>USER PROFILE</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🏷️ <b>Name:</b> <code>{full_name}</code>\n"
                f"🆔 <b>Username:</b> @{username}\n"
                f"🗝️ <b>User ID:</b> <code>{uid}</code>\n\n"
                f"💵 <b>Wallet Balance:</b> <code>{format_balance(user_data.get('balance', 0))} BDT</code>\n\n"
                f"📊 <b>Today's Activity:</b>\n"
                f"<blockquote>📱 Numbers Taken: <code>{stats['today_numbers']}</code>\n"
                f"🔑 OTPs Received: <code>{stats['today_otps']}</code></blockquote>\n\n"
                f"🌐 <b>All-Time Metrics:</b>\n"
                f"<blockquote>📱 Numbers Taken: <code>{stats['total_numbers']}</code>\n"
                f"🔑 OTPs Received: <code>{stats['total_otps']}</code></blockquote>"
            )
        await update.message.reply_text(profile_text, parse_mode="HTML", reply_markup=main_keyboard(uid))
        return

    # Generic Placeholder Fallback for Other Triggers
    if text in T_BUY:
        await update.message.reply_text("📱 Service purchase queue is ready. Select service ID.")
    elif text in T_TOPUP:
        await update.message.reply_text("💳 Contact Admin (@your_admin) to add funds to your wallet.")
    elif text in T_SUPPORT:
        await update.message.reply_text("💬 Need help? Reach out directly to support at @your_support_user.")

# ==================== MAIN APPLICATION SETUP ====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = get_user_lang(uid)
    await update.message.reply_text(
        LANG_TEXTS[lang]["welcome"],
        parse_mode="HTML",
        reply_markup=main_keyboard(uid)
    )

async def main():
    # Build Telegram Bot Application
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers Registration
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Async Background Task Loops
    asyncio.create_task(liveaccess_refresh_loop())
    asyncio.create_task(monitor_loop(app))
    asyncio.create_task(worker())

    print("🚀 Bot starting...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    
    # Keep application running indefinitely
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot execution terminated.")