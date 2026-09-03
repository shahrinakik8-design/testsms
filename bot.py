#!/usr/bin/env python3
"""
Zebra SMS API Bot
Wraps the three endpoints (getnum, getupdate, liveaccess) into a simple
interactive CLI: browse which ranges are delivering, grab a number, and
auto-poll for the incoming code.

Usage:
    python3 zebra_bot.py                 # interactive menu
    python3 zebra_bot.py ranges [sender]  # list live ranges
    python3 zebra_bot.py get <range>      # allocate a number + wait for code
"""

import sys
import time
import json
import requests

BASE_URL = "https://zebrasms.com/api/v1"
API_KEY = "6U3G3DDZ6GB"   # move to an env var if you reuse this script elsewhere

HEADERS = {
    "MAuth": API_KEY,
    "Content-Type": "application/json",
}


def _call(method, path, **kwargs):
    url = f"{BASE_URL}{path}"
    try:
        resp = requests.request(method, url, headers=HEADERS, timeout=15, **kwargs)
        data = resp.json()
    except requests.RequestException as e:
        print(f"[network error] {e}")
        return None
    except json.JSONDecodeError:
        print(f"[bad response] {resp.text[:200]}")
        return None

    meta = data.get("meta", {})
    if meta.get("code") != 0:
        print(f"[api error {meta.get('code')}] {meta.get('error')}")
        return None
    return data.get("data")


def live_ranges(sender=None):
    params = {"sender": sender} if sender else {}
    data = _call("GET", "/publicapi/liveaccess", params=params)
    if not data:
        return []
    rows = data.get("rows", [])
    print(f"\n{data.get('count', 0)} sender(s) delivering right now:\n")
    for row in rows:
        print(f"  {row['sender']:<15} -> {', '.join(row['ranges'])}")
    print()
    return rows


def get_number(rng):
    data = _call("POST", "/publicapi/getnum", json={"range": rng})
    if not data or not data.get("rows"):
        return None
    row = data["rows"][0]
    print(f"\nAllocated: {row['number']}  ({row['country']} / {row['operator']})")
    print(f"Expires:   {time.strftime('%H:%M:%S', time.localtime(row['expires_ms'] / 1000))}\n")
    return row


def get_updates():
    data = _call("GET", "/publicapi/getupdate")
    if not data:
        return []
    return data.get("rows", [])


def wait_for_code(number, expires_ms, poll_interval=3):
    """Poll getupdate until a message for `number` shows up or it expires."""
    print(f"Waiting for a code on {number} (Ctrl+C to stop)...")
    seen = set()
    # Prime `seen` so we don't report codes that arrived before we started.
    for row in get_updates():
        seen.add((row["number"], row["message"], row["at_ms"]))

    try:
        while True:
            now_ms = int(time.time() * 1000)
            if now_ms > expires_ms:
                print("Number expired with no code received.")
                return None

            for row in get_updates():
                key = (row["number"], row["message"], row["at_ms"])
                if row["number"] == number and key not in seen:
                    seen.add(key)
                    print(f"\nCode received from {row['sender']}: {row['message']}\n")
                    return row

            time.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\nStopped waiting.")
        return None


def allocate_and_wait(rng):
    row = get_number(rng)
    if not row:
        return
    wait_for_code(row["number"], row["expires_ms"])


def recent_codes():
    rows = get_updates()
    if not rows:
        print("No codes yet.")
        return
    print(f"\nLast {len(rows)} code(s):\n")
    for r in rows:
        print(f"  {r['number']:<18} {r['sender']:<10} {r['message']}")
    print()


def menu():
    while True:
        print("=" * 40)
        print("Zebra SMS Bot")
        print("=" * 40)
        print("1) Show live ranges (which senders are delivering)")
        print("2) Allocate a number and wait for its code")
        print("3) Show last 50 received codes")
        print("4) Quit")
        choice = input("> ").strip()

        if choice == "1":
            sender = input("Filter by sender (blank for all): ").strip() or None
            live_ranges(sender)
        elif choice == "2":
            rng = input("Range to allocate from (e.g. 22501XXX): ").strip()
            if rng:
                allocate_and_wait(rng)
        elif choice == "3":
            recent_codes()
        elif choice == "4":
            break
        else:
            print("Not a valid option.\n")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        menu()
    elif args[0] == "ranges":
        live_ranges(args[1] if len(args) > 1 else None)
    elif args[0] == "get" and len(args) > 1:
        allocate_and_wait(args[1])
    else:
        print(__doc__)
