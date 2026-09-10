#!/usr/bin/env python3
"""
Zebra SMS Telegram Bot — bottom keyboard UI + admin service/country manager.

Setup:
    pip install -r requirements.txt
    export TELEGRAM_BOT_TOKEN="123456:ABC-your-bot-father-token"
    export ADMIN_IDS="111111111,222222222"   # your numeric Telegram user id(s)
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
import re
import json
import time
import asyncio
import logging
import traceback
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("zebra_bot")

BASE_URL = "https://api.zebrasms.com/api/v1"
API_KEY = "6U3G3DDZ6GB"
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

HEADERS = {
    "MAuth": API_KEY,
    "Content-Type": "application/json",
    "User-Agent": "curl/8.4.0",
    "Accept": "*/*",
}

DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
SERVICES_FILE = os.path.join(DATA_DIR, "services.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

CONFIG = {"max_per_user": 1, "range_hits": {}}


def load_config():
    global CONFIG
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                CONFIG.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass


def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, indent=2)
    except OSError as e:
        print(f"Could not save config.json: {e}")

ACTIVE_NUMBERS = {}        # chat_id -> {number: {"task": Task, "message_id": int, "rng": str}}
KNOWN_USERS = set()        # chat_ids that have started the bot (in-memory only)
NUMBERS_ALLOCATED = 0
ADMIN_STATE = {}           # admin chat_id -> ("action_name", extra_data)
USER_STATE = {}            # regular chat_id -> "awaiting_custom_range"
SEEN_FEED_KEYS = set()     # (number, message, at_ms) already posted to the live-feed group
FEED_POLL_SECONDS = 5      # getupdate is cached for 3s, so polling every 5s is cheap and safe

GROUP_USERNAME = os.environ.get("GROUP_USERNAME", "-1004415108815")
GROUP_LINK = os.environ.get("GROUP_LINK", "https://t.me/otpmastersgrp")

# services = { "Facebook": { "Ivory Coast": "22501XXX", ... }, ... }
services = {}

AUTO_SERVICE_NAME = "🔎 Auto-Detected"
AUTO_POLL_SECONDS = 10  # how often to check Zebra's liveaccess endpoint in the background

# ── Calling-code -> country lookup (so country names fill in automatically) ─
# Checked longest-prefix-first (3 digits, then 2, then 1) against the start
# of a range like "22501XXX" -> "225" -> Ivory Coast.
CALLING_CODES = {
    "1": "USA/Canada", "7": "Russia/Kazakhstan", "20": "Egypt", "27": "South Africa",
    "30": "Greece", "31": "Netherlands", "32": "Belgium", "33": "France", "34": "Spain",
    "36": "Hungary", "39": "Italy", "40": "Romania", "41": "Switzerland", "43": "Austria",
    "44": "United Kingdom", "45": "Denmark", "46": "Sweden", "47": "Norway", "48": "Poland",
    "49": "Germany", "51": "Peru", "52": "Mexico", "53": "Cuba", "54": "Argentina",
    "55": "Brazil", "56": "Chile", "57": "Colombia", "58": "Venezuela", "60": "Malaysia",
    "61": "Australia", "62": "Indonesia", "63": "Philippines", "64": "New Zealand",
    "65": "Singapore", "66": "Thailand", "81": "Japan", "82": "South Korea", "84": "Vietnam",
    "86": "China", "90": "Turkey", "91": "India", "92": "Pakistan", "93": "Afghanistan",
    "94": "Sri Lanka", "95": "Myanmar", "98": "Iran",
    "211": "South Sudan", "212": "Morocco", "213": "Algeria", "216": "Tunisia",
    "218": "Libya", "220": "Gambia", "221": "Senegal", "222": "Mauritania", "223": "Mali",
    "224": "Guinea", "225": "Ivory Coast", "226": "Burkina Faso", "227": "Niger",
    "228": "Togo", "229": "Benin", "230": "Mauritius", "231": "Liberia", "232": "Sierra Leone",
    "233": "Ghana", "234": "Nigeria", "235": "Chad", "236": "Central African Republic",
    "237": "Cameroon", "238": "Cape Verde", "239": "Sao Tome and Principe",
    "240": "Equatorial Guinea", "241": "Gabon", "242": "Republic of the Congo",
    "243": "DR Congo", "244": "Angola", "245": "Guinea-Bissau", "248": "Seychelles",
    "249": "Sudan", "250": "Rwanda", "251": "Ethiopia", "252": "Somalia", "253": "Djibouti",
    "254": "Kenya", "255": "Tanzania", "256": "Uganda", "257": "Burundi", "258": "Mozambique",
    "260": "Zambia", "261": "Madagascar", "262": "Reunion/Mayotte", "263": "Zimbabwe",
    "264": "Namibia", "265": "Malawi", "266": "Lesotho", "267": "Botswana", "268": "Eswatini",
    "269": "Comoros", "290": "Saint Helena", "291": "Eritrea",
    "297": "Aruba", "298": "Faroe Islands", "299": "Greenland",
    "350": "Gibraltar", "351": "Portugal", "352": "Luxembourg", "353": "Ireland",
    "354": "Iceland", "355": "Albania", "356": "Malta", "357": "Cyprus", "358": "Finland",
    "359": "Bulgaria", "370": "Lithuania", "371": "Latvia", "372": "Estonia",
    "373": "Moldova", "374": "Armenia", "375": "Belarus", "376": "Andorra",
    "377": "Monaco", "378": "San Marino", "380": "Ukraine", "381": "Serbia",
    "382": "Montenegro", "383": "Kosovo", "385": "Croatia", "386": "Slovenia",
    "387": "Bosnia and Herzegovina", "389": "North Macedonia", "420": "Czech Republic",
    "421": "Slovakia", "423": "Liechtenstein",
    "500": "Falkland Islands", "501": "Belize", "502": "Guatemala", "503": "El Salvador",
    "504": "Honduras", "505": "Nicaragua", "506": "Costa Rica", "507": "Panama",
    "508": "Saint Pierre and Miquelon", "509": "Haiti", "590": "Guadeloupe",
    "591": "Bolivia", "592": "Guyana", "593": "Ecuador", "594": "French Guiana",
    "595": "Paraguay", "596": "Martinique", "597": "Suriname", "598": "Uruguay",
    "599": "Curacao",
    "670": "East Timor", "672": "Norfolk Island", "673": "Brunei", "674": "Nauru",
    "675": "Papua New Guinea", "676": "Tonga", "677": "Solomon Islands", "678": "Vanuatu",
    "679": "Fiji", "680": "Palau", "681": "Wallis and Futuna", "682": "Cook Islands",
    "683": "Niue", "685": "Samoa", "686": "Kiribati", "687": "New Caledonia",
    "688": "Tuvalu", "689": "French Polynesia", "690": "Tokelau", "691": "Micronesia",
    "692": "Marshall Islands",
    "850": "North Korea", "852": "Hong Kong", "853": "Macau", "855": "Cambodia",
    "856": "Laos", "870": "Inmarsat", "880": "Bangladesh", "886": "Taiwan",
    "960": "Maldives", "961": "Lebanon", "962": "Jordan", "963": "Syria", "964": "Iraq",
    "965": "Kuwait", "966": "Saudi Arabia", "967": "Yemen", "968": "Oman",
    "970": "Palestine", "971": "United Arab Emirates", "972": "Israel", "973": "Bahrain",
    "974": "Qatar", "975": "Bhutan", "976": "Mongolia", "977": "Nepal", "992": "Tajikistan",
    "993": "Turkmenistan", "994": "Azerbaijan", "995": "Georgia", "996": "Kyrgyzstan",
    "998": "Uzbekistan",
}


def guess_country_from_range(rng):
    """Try the longest calling-code prefix (3, then 2, then 1 digits) of a range."""
    digits = "".join(ch for ch in rng if ch.isdigit())
    for length in (3, 2, 1):
        prefix = digits[:length]
        if prefix in CALLING_CODES:
            return CALLING_CODES[prefix]
    return None


# Country name -> ISO2, used only to render a flag emoji next to a country name.
# Best-effort / not exhaustive — unknown countries just show without a flag.
COUNTRY_TO_ISO2 = {
    "USA/Canada": "US", "Russia/Kazakhstan": "RU", "Egypt": "EG", "South Africa": "ZA",
    "Greece": "GR", "Netherlands": "NL", "Belgium": "BE", "France": "FR", "Spain": "ES",
    "Hungary": "HU", "Italy": "IT", "Romania": "RO", "Switzerland": "CH", "Austria": "AT",
    "United Kingdom": "GB", "Denmark": "DK", "Sweden": "SE", "Norway": "NO", "Poland": "PL",
    "Germany": "DE", "Peru": "PE", "Mexico": "MX", "Cuba": "CU", "Argentina": "AR",
    "Brazil": "BR", "Chile": "CL", "Colombia": "CO", "Venezuela": "VE", "Malaysia": "MY",
    "Australia": "AU", "Indonesia": "ID", "Philippines": "PH", "New Zealand": "NZ",
    "Singapore": "SG", "Thailand": "TH", "Japan": "JP", "South Korea": "KR", "Vietnam": "VN",
    "China": "CN", "Turkey": "TR", "India": "IN", "Pakistan": "PK", "Afghanistan": "AF",
    "Sri Lanka": "LK", "Myanmar": "MM", "Iran": "IR",
    "South Sudan": "SS", "Morocco": "MA", "Algeria": "DZ", "Tunisia": "TN", "Libya": "LY",
    "Gambia": "GM", "Senegal": "SN", "Mauritania": "MR", "Mali": "ML", "Guinea": "GN",
    "Ivory Coast": "CI", "Burkina Faso": "BF", "Niger": "NE", "Togo": "TG", "Benin": "BJ",
    "Mauritius": "MU", "Liberia": "LR", "Sierra Leone": "SL", "Ghana": "GH", "Nigeria": "NG",
    "Chad": "TD", "Central African Republic": "CF", "Cameroon": "CM", "Cape Verde": "CV",
    "Sao Tome and Principe": "ST", "Equatorial Guinea": "GQ", "Gabon": "GA",
    "Republic of the Congo": "CG", "DR Congo": "CD", "Angola": "AO", "Guinea-Bissau": "GW",
    "Seychelles": "SC", "Sudan": "SD", "Rwanda": "RW", "Ethiopia": "ET", "Somalia": "SO",
    "Djibouti": "DJ", "Kenya": "KE", "Tanzania": "TZ", "Uganda": "UG", "Burundi": "BI",
    "Mozambique": "MZ", "Zambia": "ZM", "Madagascar": "MG", "Reunion/Mayotte": "RE",
    "Zimbabwe": "ZW", "Namibia": "NA", "Malawi": "MW", "Lesotho": "LS", "Botswana": "BW",
    "Eswatini": "SZ", "Comoros": "KM", "Saint Helena": "SH", "Eritrea": "ER",
    "Aruba": "AW", "Faroe Islands": "FO", "Greenland": "GL",
    "Gibraltar": "GI", "Portugal": "PT", "Luxembourg": "LU", "Ireland": "IE",
    "Iceland": "IS", "Albania": "AL", "Malta": "MT", "Cyprus": "CY", "Finland": "FI",
    "Bulgaria": "BG", "Lithuania": "LT", "Latvia": "LV", "Estonia": "EE",
    "Moldova": "MD", "Armenia": "AM", "Belarus": "BY", "Andorra": "AD",
    "Monaco": "MC", "San Marino": "SM", "Ukraine": "UA", "Serbia": "RS",
    "Montenegro": "ME", "Kosovo": "XK", "Croatia": "HR", "Slovenia": "SI",
    "Bosnia and Herzegovina": "BA", "North Macedonia": "MK", "Czech Republic": "CZ",
    "Slovakia": "SK", "Liechtenstein": "LI",
    "Falkland Islands": "FK", "Belize": "BZ", "Guatemala": "GT", "El Salvador": "SV",
    "Honduras": "HN", "Nicaragua": "NI", "Costa Rica": "CR", "Panama": "PA",
    "Saint Pierre and Miquelon": "PM", "Haiti": "HT", "Guadeloupe": "GP",
    "Bolivia": "BO", "Guyana": "GY", "Ecuador": "EC", "French Guiana": "GF",
    "Paraguay": "PY", "Martinique": "MQ", "Suriname": "SR", "Uruguay": "UY",
    "Curacao": "CW",
    "East Timor": "TL", "Norfolk Island": "NF", "Brunei": "BN", "Nauru": "NR",
    "Papua New Guinea": "PG", "Tonga": "TO", "Solomon Islands": "SB", "Vanuatu": "VU",
    "Fiji": "FJ", "Palau": "PW", "Wallis and Futuna": "WF", "Cook Islands": "CK",
    "Niue": "NU", "Samoa": "WS", "Kiribati": "KI", "New Caledonia": "NC",
    "Tuvalu": "TV", "French Polynesia": "PF", "Tokelau": "TK", "Micronesia": "FM",
    "Marshall Islands": "MH",
    "North Korea": "KP", "Hong Kong": "HK", "Macau": "MO", "Cambodia": "KH",
    "Laos": "LA", "Bangladesh": "BD", "Taiwan": "TW",
    "Maldives": "MV", "Lebanon": "LB", "Jordan": "JO", "Syria": "SY", "Iraq": "IQ",
    "Kuwait": "KW", "Saudi Arabia": "SA", "Yemen": "YE", "Oman": "OM",
    "Palestine": "PS", "United Arab Emirates": "AE", "Israel": "IL", "Bahrain": "BH",
    "Qatar": "QA", "Bhutan": "BT", "Mongolia": "MN", "Nepal": "NP", "Tajikistan": "TJ",
    "Turkmenistan": "TM", "Azerbaijan": "AZ", "Georgia": "GE", "Kyrgyzstan": "KG",
    "Uzbekistan": "UZ",
}


def flag_emoji(country_name):
    iso2 = COUNTRY_TO_ISO2.get(country_name)
    if not iso2 or len(iso2) != 2:
        return ""
    return "".join(chr(127397 + ord(ch)) for ch in iso2.upper())


SERVICE_KEYWORDS = [
    "facebook", "instagram", "whatsapp", "telegram", "google", "tiktok", "twitter",
    "snapchat", "viber", "wechat", "uber", "paypal", "amazon", "netflix",
    "microsoft", "apple", "spotify", "discord", "tinder", "line", "grab", "gojek",
]


def detect_service(message):
    lower = message.lower()
    for kw in SERVICE_KEYWORDS:
        if kw in lower:
            return kw.capitalize()
    return None


def find_known_range_for_country(country):
    """Only returns a range if we've genuinely saved one for this country before
    (via admin manual entry or auto-detection) — never fabricated, so it's always
    safe to copy into Custom Range."""
    for countries in services.values():
        for name, rng in countries.items():
            if name.split(" (")[0] == country:  # strip any "(2)" dedup suffix
                return rng
    return None


def mask_code_in_message(message, code):
    if not code:
        return message
    return message.replace(code, "*" * len(code))


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
    url = f"{BASE_URL}{path}"
    resp = requests.request(method, url, headers=HEADERS, timeout=15, **kwargs)
    logger.info(
        "Zebra API call: %s %s | body=%s | HTTP %s | response=%s",
        method, url, kwargs.get("json") or kwargs.get("params"), resp.status_code, resp.text[:500],
    )
    try:
        data = resp.json()
    except ValueError:
        snippet = resp.text[:200].strip() or "(empty response)"
        raise RuntimeError(f"API returned a non-JSON response (HTTP {resp.status_code}): {snippet}")
    meta = data.get("meta", {})
    if meta.get("code") != 0:
        raise RuntimeError(data.get("message") or meta.get("error") or f"API error {meta.get('code')}")
    return data.get("data")


async def api_call(method, path, **kwargs):
    return await asyncio.to_thread(_call_sync, method, path, **kwargs)


def is_admin(user_id):
    return user_id in ADMIN_IDS


def extract_code(message):
    """Best-effort pull of just the OTP code out of a full SMS body, so we can
    show it in its own copyable snippet instead of the whole message."""
    match = re.search(r"\b\d{3,8}\b", message)
    if match:
        return match.group(0)
    match = re.search(r"\b[A-Za-z0-9]{4,8}\b", message)
    return match.group(0) if match else None


async def post_to_group(context, text):
    """Best-effort mirror of a message to the configured Telegram group.
    Never raises — a failure here should never break the main flow."""
    if not GROUP_USERNAME:
        return
    try:
        await context.bot.send_message(GROUP_USERNAME, text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.info("post_to_group failed: %s", e)


# ── Bottom (persistent) keyboard ────────────────────────────────────────

USER_ROWS = [["📱 Get Number", "📡 Browse Ranges"], ["📨 My Codes", "🔴 Live Feed"], ["ℹ️ Help"]]


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

async def show_services(update_or_query, edit=False, user_id=None):
    custom_btn = [InlineKeyboardButton("✍️ Custom Range", callback_data="customrange")]
    visible_services = {
        name: c for name, c in services.items() if name != AUTO_SERVICE_NAME or is_admin(user_id)
    }

    if not visible_services:
        text = "No services configured yet. Enter a custom range yourself, or ask an admin to add a service."
        kb = InlineKeyboardMarkup([custom_btn])
        if edit:
            await update_or_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            await update_or_query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    buttons = [[InlineKeyboardButton(name, callback_data=f"svc:{name}")] for name in visible_services.keys()]
    buttons.append(custom_btn)
    text = "📱 *Choose a service:*"
    if edit:
        await update_or_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update_or_query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))


async def show_countries(query, service):
    countries = services.get(service, {})
    back_btn = InlineKeyboardButton("⬅️ Back", callback_data="svcback")
    if not countries:
        await query.edit_message_text(
            f"No countries configured for {service} yet.", reply_markup=InlineKeyboardMarkup([[back_btn]])
        )
        return

    hits = CONFIG.get("range_hits", {})

    def base_name(country):
        return country.split(" (")[0]

    groups = {}
    for country, rng in countries.items():
        groups.setdefault(base_name(country), []).append((country, rng))
    for group in groups.values():
        group.sort(key=lambda cr: hits.get(cr[1], 0), reverse=True)
    ordered_groups = sorted(groups.items(), key=lambda kv: sum(hits.get(r, 0) for _, r in kv[1]), reverse=True)
    ordered = [cr for _, group in ordered_groups for cr in group]

    buttons = [
        [InlineKeyboardButton(country, callback_data=f"svccountry:{service}:{country}")]
        for country, _rng in ordered
    ]
    buttons.append([back_btn])
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

    if data == "customrange":
        USER_STATE[chat_id] = "awaiting_custom_range"
        await query.edit_message_text(
            "✍️ Send the range you want a number from, e.g. `22501XXX`.\nSend /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "svcback":
        await show_services(query, edit=True, user_id=query.from_user.id)

    elif data.startswith("svc:"):
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
    except Exception as e:
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
    back_btn = InlineKeyboardButton("⬅️ Back", callback_data="backtosenders")
    try:
        data = await api_call("GET", "/publicapi/liveaccess", params={"sender": sender})
    except Exception as e:
        await query.edit_message_text(f"⚠️ Error: {e}", reply_markup=InlineKeyboardMarkup([[back_btn]]))
        return
    rows = data.get("rows", [])
    ranges = rows[0]["ranges"] if rows else []
    if not ranges:
        await query.edit_message_text(f"No ranges found for {sender}.", reply_markup=InlineKeyboardMarkup([[back_btn]]))
        return
    buttons = [[InlineKeyboardButton(rng, callback_data=f"getnum:{rng}")] for rng in ranges]
    buttons.append([back_btn])
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

    elif data == "backtosenders":
        await show_sender_list(query, edit=True)

    elif data.startswith("getnum:"):
        payload = data.split(":", 1)[1]
        if "|" in payload:
            rng, label = payload.split("|", 1)
        else:
            rng, label = payload, None
        await start_getnum_flow(chat_id, rng, context, edit_query=query, label=label)

    elif data.startswith("delnum:"):
        number = data.split(":", 1)[1]
        ACTIVE_NUMBERS.get(chat_id, {}).pop(number, None)
        try:
            await query.edit_message_text(f"🗑 Deleted `{number}`.", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass


# ── Get number + wait for code (core logic, reused everywhere) ─────────

async def getnum_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    KNOWN_USERS.add(chat_id)
    if not context.args:
        await update.message.reply_text("Usage: /getnum 22501XXX")
        return
    await start_getnum_flow(chat_id, context.args[0], context)


def number_card_kb(rng, number, service=None, label=None):
    change_data = f"getnum:{rng}|{label}" if label else f"getnum:{rng}"
    rows = [[
        InlineKeyboardButton("🔄 Change Number", callback_data=change_data),
        InlineKeyboardButton("🗑 Delete Number", callback_data=f"delnum:{number}"),
    ]]
    if service and service in services:
        rows.append([InlineKeyboardButton("🌍 Change Country", callback_data=f"svc:{service}")])
    if GROUP_LINK:
        rows.append([InlineKeyboardButton("🔍 OTP GRUP 🔍", url=GROUP_LINK)])
    return InlineKeyboardMarkup(rows)


async def start_getnum_flow(chat_id, rng, context, edit_query=None, label=None):
    global NUMBERS_ALLOCATED

    active = ACTIVE_NUMBERS.setdefault(chat_id, {})
    limit = CONFIG.get("max_per_user", 1)
    if len(active) >= limit:
        msg = f"⏳ You already have {limit} active number(s) — cancel one with /cancel first, or wait for it to finish."
        if edit_query:
            await edit_query.edit_message_text(msg)
        else:
            await context.bot.send_message(chat_id, msg)
        return

    try:
        data = await api_call("POST", "/publicapi/getnum", json={"range": rng})
    except Exception as e:
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

    service = label.split(" / ")[0] if label else None
    flag = flag_emoji(row.get("country", ""))

    header = f"*{label}*\n" if label else ""
    text = (
        f"📱 *Number Assigned*\n\n"
        f"{header}"
        f"{flag} `{number}`\n"
        f"🌍 {row['country']} • {row['operator']}\n"
        f"⏰ Expires at {expires_str}\n\n"
        f"⌛ Waiting for the code... (/cancel to stop)"
    )
    kb = number_card_kb(rng, number, service, label)
    if edit_query:
        sent = await edit_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        sent = await context.bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    message_id = getattr(sent, "message_id", None)

    # No per-number polling loop anymore — a single shared background job
    # (central_updates_poller) checks getupdate once per cycle and delivers
    # to every active number that matches, which is what keeps the bot fast
    # even with many numbers active at once.
    ACTIVE_NUMBERS[chat_id][number] = {
        "message_id": message_id, "rng": rng, "label": label,
        "expires_ms": expires_ms, "delivered": False,
    }


async def _update_status(chat_id, number, context, text, reply_markup=None):
    """Edits this number's tracked status message in place if we have one."""
    info = ACTIVE_NUMBERS.get(chat_id, {}).get(number)
    message_id = info["message_id"] if info else None
    if message_id is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text,
                parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup,
            )
            return message_id
        except Exception:
            pass
    sent = await context.bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
    return getattr(sent, "message_id", None)


async def _vanish_later(context, chat_id, message_id, delay=60):
    """Deletes the given message after a delay, for a clean 'vanish' effect."""
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id, message_id)
    except Exception:
        pass


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cleared = False

    if USER_STATE.pop(chat_id, None) is not None:
        cleared = True

    if ACTIVE_NUMBERS.get(chat_id):
        ACTIVE_NUMBERS[chat_id].clear()
        cleared = True

    if cleared:
        await update.message.reply_text("🛑 Stopped / cancelled.")
    else:
        await update.message.reply_text("Nothing in progress.")


# ── Codes list ───────────────────────────────────────────────────────────

async def codes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    KNOWN_USERS.add(update.effective_chat.id)
    try:
        data = await api_call("GET", "/publicapi/getupdate")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")
        return
    rows = data.get("rows", [])[:15]
    if not rows:
        await update.message.reply_text("No codes received yet.")
        return
    lines = [f"{r['number']} ({r['sender']}): {r['message']}" for r in rows]
    await update.message.reply_text("\n".join(lines))


async def live_feed_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    KNOWN_USERS.add(update.effective_chat.id)

    if not services:
        text = "No verified ranges saved yet."
    else:
        lines = []
        for name, countries in services.items():
            if not countries:
                continue
            lines.append(f"*{name}*")
            for country, rng in countries.items():
                flag = flag_emoji(country.split(" (")[0])
                lines.append(f"  • {country} {flag} — `{rng}`".rstrip())
        text = "📡 *Known working ranges:*\n\n" + "\n".join(lines) if lines else "No verified ranges saved yet."

    text += "\n\n🔴 Live incoming codes (masked) are posted in real time in our group."
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("Open Group", url=GROUP_LINK)]]) if GROUP_LINK else None
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=buttons)


HELP_TEXT = (
    "ℹ️ *How it works*\n\n"
    "📱 *Get Number* — pick a service, then a country (or a custom range); the "
    "bot allocates a number and waits for its code automatically\n"
    "📡 *Browse Ranges* — see raw ranges by sender id (advanced/manual)\n"
    "📨 *My Codes* — your last received codes\n"
    "🔴 *Live Feed* — known working ranges, plus a link to the live group feed\n\n"
    "You can also type `/getnum 22501XXX` directly."
)


# ── Admin panel ──────────────────────────────────────────────────────────

def admin_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data="adm:stats")],
        [InlineKeyboardButton("🧩 Manage Services", callback_data="adm:services")],
        [InlineKeyboardButton(f"🔢 Per-user limit: {CONFIG.get('max_per_user', 1)}", callback_data="adm:setlimit")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="adm:broadcast")],
        [InlineKeyboardButton("⬅️ Close", callback_data="adm:close")],
    ])


async def show_admin_sender_list(query, service):
    try:
        data = await api_call("GET", "/publicapi/liveaccess")
    except Exception as e:
        await query.edit_message_text(f"⚠️ Error: {e}", reply_markup=services_menu_kb())
        return
    rows = data.get("rows", [])
    manual_btn = [InlineKeyboardButton("✍️ Enter Range Manually", callback_data=f"adm:acrmanual:{service}")]
    if not rows:
        buttons = [manual_btn, [InlineKeyboardButton("⬅️ Back", callback_data="adm:services")]]
        await query.edit_message_text(
            "No senders delivering right now, but you can still add a range you already know:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return
    buttons = [
        [InlineKeyboardButton(f"{r['sender']} ({len(r['ranges'])})", callback_data=f"adm:acrsender:{service}:{r['sender']}")]
        for r in rows[:30]
    ]
    buttons.append(manual_btn)
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm:services")])
    await query.edit_message_text(
        f"📡 Pick a sender for *{service}*:", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_admin_ranges_for_sender(query, service, sender):
    try:
        data = await api_call("GET", "/publicapi/liveaccess", params={"sender": sender})
    except Exception as e:
        await query.edit_message_text(f"⚠️ Error: {e}", reply_markup=services_menu_kb())
        return
    rows = data.get("rows", [])
    ranges = rows[0]["ranges"] if rows else []
    if not ranges:
        await query.edit_message_text(f"No ranges found for {sender}.", reply_markup=services_menu_kb())
        return
    buttons = [
        [InlineKeyboardButton(f"{rng} → {guess_country_from_range(rng) or '?'}", callback_data=f"adm:acrrange:{service}:{rng}")]
        for rng in ranges
    ]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"adm:svcpick:{service}")])
    await query.edit_message_text(
        f"📡 *{sender}* — tap a range to auto-add it to *{service}*:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def _range_already_known(rng):
    """True if this exact range is already saved under any service."""
    for countries in services.values():
        if rng in countries.values():
            return True
    return False


async def central_updates_poller(context: ContextTypes.DEFAULT_TYPE):
    """The ONE place that polls getupdate. Replaces per-number polling loops —
    this is what keeps the bot fast even with many numbers active at once.

    Each poll cycle:
      1. Fetches getupdate once.
      2. Delivers the code to any active number waiting on it (updates that
         card, posts a copy to the group, schedules the vanish-delete).
      3. Anything not matching an active number goes to the group as a masked
         (code-hidden) live-activity post instead.
      4. Also expires any active number whose time ran out.
    """
    try:
        data = await api_call("GET", "/publicapi/getupdate")
    except Exception as e:
        logger.info("central_updates_poller: getupdate call failed (%s) — will retry next cycle.", e)
        data = None

    rows = data.get("rows", []) if data else []
    first_run = not SEEN_FEED_KEYS

    # number -> list of chat_ids currently waiting on that exact number
    number_index = {}
    for chat_id, nums in ACTIVE_NUMBERS.items():
        for number in nums:
            number_index.setdefault(number, []).append(chat_id)

    for r in reversed(rows):  # oldest-first so the feed reads chronologically
        key = (r["number"], r["message"], r["at_ms"])
        if key in SEEN_FEED_KEYS:
            continue
        SEEN_FEED_KEYS.add(key)
        if first_run:
            continue  # don't dump the existing backlog on startup

        delivered = False
        for chat_id in number_index.get(r["number"], []):
            info = ACTIVE_NUMBERS.get(chat_id, {}).get(r["number"])
            if not info or info.get("delivered"):
                continue
            info["delivered"] = True
            delivered = True

            number = r["number"]
            rng, label = info["rng"], info.get("label")
            service = label.split(" / ")[0] if label else None
            kb = number_card_kb(rng, number, service, label)
            code = extract_code(r["message"])
            header = f"*{label}*\n" if label else ""
            text = f"🎉 *SMS Received*\n\n{header}📞 `{number}`\n💬 {r['message']}"
            if code:
                text += f"\n\n🔑 *CODE*\n```\n{code}\n```"
            msg_id = await _update_status(chat_id, number, context, text, reply_markup=kb)

            group_text = f"📨 *{r['sender']}* — `{number}`\n{r['message']}"
            if code:
                group_text += f"\n*Code:* `{code}`"
            await post_to_group(context, group_text)

            if msg_id is not None:
                asyncio.create_task(_vanish_later(context, chat_id, msg_id, delay=60))
            ACTIVE_NUMBERS[chat_id].pop(number, None)

        if not delivered:
            code = extract_code(r["message"])
            masked = mask_code_in_message(r["message"], code)
            service = detect_service(r["message"]) or "Unknown"
            country = r.get("country") or "Unknown"
            flag = flag_emoji(country)
            known_range = find_known_range_for_country(country)

            lines = ["🆕 *New Activity*", f"⚙️ Service: {service}", f"🌍 Country: {country} {flag}".strip()]
            if known_range:
                lines.append(f"📱 Range: `{known_range}`")
            lines.append(f"✉️ Full SMS:\n{masked}")
            await post_to_group(context, "\n".join(lines))

    if len(SEEN_FEED_KEYS) > 5000:
        SEEN_FEED_KEYS.clear()

    # Expire any active number whose time ran out and never got a code.
    now_ms = int(time.time() * 1000)
    for chat_id, nums in list(ACTIVE_NUMBERS.items()):
        for number, info in list(nums.items()):
            if now_ms > info["expires_ms"]:
                rng, label = info["rng"], info.get("label")
                service = label.split(" / ")[0] if label else None
                kb = number_card_kb(rng, number, service, label)
                await _update_status(chat_id, number, context, f"⌛ `{number}` expired with no code received.", reply_markup=kb)
                nums.pop(number, None)


async def auto_poll_liveaccess(context: ContextTypes.DEFAULT_TYPE):
    """Background job: checks Zebra's liveaccess endpoint every few minutes and
    self-adds any new range it finds under AUTO_SERVICE_NAME. Silently does
    nothing if liveaccess returns no data (e.g. while it's broken upstream) —
    it'll just start working the moment Zebra's endpoint starts returning rows."""
    try:
        data = await api_call("GET", "/publicapi/liveaccess")
    except Exception as e:
        logger.info("auto_poll_liveaccess: liveaccess call failed (%s) — will retry next cycle.", e)
        return

    rows = data.get("rows", []) if data else []
    if not rows:
        return  # nothing live right now — this is normal, not an error

    newly_added = []
    services.setdefault(AUTO_SERVICE_NAME, {})
    hits = CONFIG.setdefault("range_hits", {})

    for row in rows:
        for rng in row.get("ranges", []):
            hits[rng] = hits.get(rng, 0) + 1  # track how often this range shows up live
            if _range_already_known(rng):
                continue  # already saved somewhere (auto or manual) — don't duplicate
            country = guess_country_from_range(rng) or rng
            base_country = country
            n = 2
            while country in services[AUTO_SERVICE_NAME]:
                country = f"{base_country} ({n})"
                n += 1
            services[AUTO_SERVICE_NAME][country] = rng
            newly_added.append(f"{country} → {rng}")

    save_config()
    if newly_added:
        save_services()
        text = "🔎 *Auto-detected new range(s):*\n\n" + "\n".join(f"• {line}" for line in newly_added)
        await post_to_group(context, text)


def services_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Service", callback_data="adm:svc_add")],
        [InlineKeyboardButton("🌍 Add Country to Service", callback_data="adm:svc_addcountry")],
        [InlineKeyboardButton("📋 List Services", callback_data="adm:svc_list")],
        [InlineKeyboardButton("🗑 Remove Service", callback_data="adm:svc_remove")],
        [InlineKeyboardButton("🗑 Remove Country", callback_data="adm:svc_remcountry")],
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
            f"⌛ Active waits: {sum(len(v) for v in ACTIVE_NUMBERS.values())}\n"
            f"📱 Numbers allocated this run: {NUMBERS_ALLOCATED}\n"
            f"🧩 Services configured: {len(services)}\n\n"
            "_Counts reset if the bot restarts (in-memory only)._"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu_kb())

    elif data == "adm:broadcast":
        ADMIN_STATE[chat_id] = ("broadcast", None)
        await query.edit_message_text("📢 Send the message to broadcast to all known users.\nSend /cancel to abort.")

    elif data == "adm:setlimit":
        ADMIN_STATE[chat_id] = ("set_limit", None)
        await query.edit_message_text(
            f"🔢 Current limit: {CONFIG.get('max_per_user', 1)} number(s) per user at a time.\n"
            f"Send a new number to change it.\nSend /cancel to abort.",
        )

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
        await show_admin_sender_list(query, service)

    elif data.startswith("adm:acrsender:"):
        _, _, service, sender = data.split(":", 3)
        await show_admin_ranges_for_sender(query, service, sender)

    elif data.startswith("adm:acrmanual:"):
        service = data.split(":", 2)[2]
        ADMIN_STATE[chat_id] = ("add_country_manual", service)
        await query.edit_message_text(
            f"✍️ Send the country and range for *{service}* like this:\n`Ivory Coast 22501XXX`\n\nSend /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data.startswith("adm:acrrange:"):
        _, _, service, rng = data.split(":", 3)
        if service not in services:
            await query.edit_message_text("That service no longer exists.", reply_markup=services_menu_kb())
            return
        country = guess_country_from_range(rng) or rng  # fall back to the range itself if unknown
        # avoid silently overwriting a different range already saved under the same country name
        suffix = ""
        base_country = country
        n = 2
        while country in services[service] and services[service][country] != rng:
            country = f"{base_country} ({n})"
            n += 1
        services[service][country] = rng
        save_services()
        await query.edit_message_text(
            f"✅ Auto-added *{country}* → `{rng}` under *{service}*.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=services_menu_kb(),
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

    elif data == "adm:svc_remcountry":
        if not services:
            await query.edit_message_text("No services yet.", reply_markup=services_menu_kb())
            return
        buttons = [[InlineKeyboardButton(name, callback_data=f"adm:remcountrysvc:{name}")] for name in services.keys()]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm:services")])
        await query.edit_message_text("Pick the service to remove a country from:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("adm:remcountrysvc:"):
        service = data.split(":", 2)[2]
        countries = services.get(service, {})
        if not countries:
            await query.edit_message_text(f"No countries under {service}.", reply_markup=services_menu_kb())
            return
        buttons = [
            [InlineKeyboardButton(f"🗑 {country}", callback_data=f"adm:remcountry:{service}:{country}")]
            for country in countries.keys()
        ]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm:svc_remcountry")])
        await query.edit_message_text(f"Pick a country to remove from *{service}*:", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("adm:remcountry:"):
        _, _, service, country = data.split(":", 3)
        services.get(service, {}).pop(country, None)
        save_services()
        await query.edit_message_text(
            f"🗑 Removed *{country}* from *{service}*.", parse_mode=ParseMode.MARKDOWN, reply_markup=services_menu_kb()
        )


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

    elif action == "set_limit":
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text("Send a whole number of 1 or more.")
            return True
        CONFIG["max_per_user"] = int(text)
        save_config()
        await update.message.reply_text(f"✅ Per-user limit set to {text}.")

    elif action == "add_country_manual":
        service = extra
        if service not in services:
            await update.message.reply_text("That service no longer exists.")
            return True
        if " " not in text:
            await update.message.reply_text(
                "Format: `Country Name RANGECODE` — try again from the admin panel.", parse_mode=ParseMode.MARKDOWN
            )
            return True
        country, rng = text.rsplit(" ", 1)
        services[service][country.strip()] = rng.strip()
        save_services()
        await update.message.reply_text(
            f"✅ Added {country.strip()} → `{rng.strip()}` under {service}.", parse_mode=ParseMode.MARKDOWN
        )

    return True


# ── Bottom-keyboard text router ─────────────────────────────────────────

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    KNOWN_USERS.add(chat_id)

    # Admin multi-step flows take priority over button labels
    if await admin_text_handler(update, context):
        return

    # Regular-user "custom range" entry takes priority over button labels too
    if USER_STATE.get(chat_id) == "awaiting_custom_range":
        USER_STATE.pop(chat_id, None)
        rng = update.message.text.strip()
        await start_getnum_flow(chat_id, rng, context)
        return

    text = update.message.text.strip()

    if text == "📱 Get Number":
        await show_services(update, user_id=update.effective_user.id)
    elif text == "📡 Browse Ranges":
        await show_sender_list(update)
    elif text == "📨 My Codes":
        await codes_command(update, context)
    elif text == "🔴 Live Feed":
        await live_feed_command(update, context)
    elif text == "ℹ️ Help":
        await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)
    elif text == "🛠 Admin":
        await admin_command(update, context)
    # else: not a recognized button — ignore silently


# ── Wiring ───────────────────────────────────────────────────────────────

async def global_error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Logs the full traceback and, where possible, tells the user something went wrong
    instead of leaving them staring at a button that did nothing."""
    logger.error("Unhandled exception:\n%s", "".join(traceback.format_exception(None, context.error, context.error.__traceback__)))

    chat_id = None
    try:
        if isinstance(update, Update):
            if update.effective_chat:
                chat_id = update.effective_chat.id
    except Exception:
        pass

    if chat_id is not None:
        try:
            await context.bot.send_message(chat_id, "⚠️ Something went wrong handling that. Please try again.")
        except Exception:
            pass


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN first.")
    if not ADMIN_IDS:
        print("⚠️  No ADMIN_IDS set — the Admin button/menu will be unusable until you set that env var.")

    load_services()
    load_config()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("getnum", getnum_command))
    app.add_handler(CommandHandler("codes", codes_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("admin", admin_command))

    app.add_handler(CallbackQueryHandler(admin_callback_router, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(service_callback_router, pattern=r"^(svc|customrange)"))
    app.add_handler(CallbackQueryHandler(raw_callback_router, pattern=r"^(sender:|getnum:|delnum:|backtosenders)"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_error_handler(global_error_handler)

    if app.job_queue is not None:
        app.job_queue.run_repeating(auto_poll_liveaccess, interval=AUTO_POLL_SECONDS, first=15)
        app.job_queue.run_repeating(central_updates_poller, interval=FEED_POLL_SECONDS, first=10)
    else:
        print("⚠️  JobQueue not available — install with 'pip install \"python-telegram-bot[job-queue]\"' for auto-detection.")

    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
