"""
CLUELESS US -- Darvas Digest
=============================
One consolidated Telegram message per scan run.
Replaces the per-alert spam from streak_tracker.py.
Compact mobile-friendly format.
"""
import os, json
from datetime import datetime
import requests


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
SEND_TELEGRAM = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DASHBOARD_URL = "https://jorawar546singh-ctrl.github.io/ttm-scanner/"


def script_path(name):
    return os.path.join(SCRIPT_DIR, name)


def load_json(path, default):
    p = script_path(path)
    if not os.path.exists(p):
        return default
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return default


def send_telegram(message):
    if not SEND_TELEGRAM:
        print("  (Telegram env vars missing -- skipping push)")
        print(message)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                  "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=10)
        if r.status_code != 200:
            print(f"  Telegram send failed ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        print(f"  Telegram send error: {e}")


def build_digest():
    today = datetime.now().strftime("%a %b %d")
    lines = [f"\U0001F4C5 *DARVAS DIGEST  {today}*"]
    lines.append("")

    # --- Section 1: today's new breakouts ---
    latest = load_json("latest.json", {"hits": []})
    hits = latest.get("hits", [])
    if hits:
        lines.append(f"*NEW BREAKOUTS ({len(hits)})*")
        for h in hits[:10]:
            sym = h["ticker"]
            price = h.get("price", 0) or 0
            vol_ratio = h.get("vol_ratio", 0) or 0
            pct = h.get("pct_above_box", 0) or 0
            lines.append(f"`{sym:<6}` ${price:<7.2f} {vol_ratio:.1f}x  {pct:+.1f}%")
        if len(hits) > 10:
            lines.append(f"\n+{len(hits) - 10} more on dashboard")
    else:
        lines.append("*NEW BREAKOUTS (0)*")
    lines.append("")

    # --- Section 2: trending (streak >= 2, sorted by streak desc) ---
    streaks_state = load_json("streaks_state.json", {"streaks": []})
    streaks = streaks_state.get("streaks", [])

    trending = sorted(
        [s for s in streaks if s.get("status") == "trending"
         and s.get("current_streak", 0) >= 2],
        key=lambda x: x.get("current_streak", 0),
        reverse=True
    )
    fading = sorted(
        [s for s in streaks if s.get("status") == "fading"],
        key=lambda x: x.get("pct_from_breakout", 0),
        reverse=True
    )

    SHOW_TRENDING = 5
    SHOW_FADING = 5

    if trending:
        lines.append(f"*TRENDING quiet ({SHOW_TRENDING} of {len(trending)})*")
        for s in trending[:SHOW_TRENDING]:
            sym = s["ticker"]
            d = s.get("days_since_breakout", 0)
            streak = s.get("current_streak", 0)
            pct = s.get("pct_from_breakout", 0) or 0
            lines.append(f"`{sym:<6}` d{d:<3} s{streak:<3}  {pct:+.1f}%")
    else:
        lines.append("*TRENDING:* none yet")
    lines.append("")

    if fading:
        lines.append(f"*FADING ({len(fading)})*")
        for s in fading[:SHOW_FADING]:
            sym = s["ticker"]
            d = s.get("days_since_breakout", 0)
            peak = s.get("peak_streak", 0)
            streak = s.get("current_streak", 0)
            pct = s.get("pct_from_breakout", 0) or 0
            lines.append(f"`{sym:<6}` d{d:<3} {peak}->{streak:<3}  {pct:+.1f}%")
        remainder = len(fading) - SHOW_FADING
        if remainder > 0:
            lines.append(f"\n+{remainder} more on dashboard")
    lines.append("")

    lines.append(f"[Dashboard]({DASHBOARD_URL})")

    return "\n".join(lines)


def main():
    print("\n>>> Building Darvas Digest...\n")
    digest = build_digest()
    print(digest)
    print()
    send_telegram(digest)
    print("\nDigest sent.\n")


if __name__ == "__main__":
    main()
