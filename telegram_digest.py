"""
Telegram EOD digest, v6 — decisions, not data, readable on a phone.

Format goal: tell me what to DO, then let me close the app.
  1. HOLDING — position(s) first, with a verdict: distance to stop + tier.
  2. NEW TODAY — shortlist signals that fired TODAY (age_days==0). Always
     shown in full, no cap — these are the actual new opportunities.
  3. STILL QUALIFYING — older shortlist signals still meeting the bar
     (kept visible past 30d by the winners-persistence fix). Capped at
     CARRY_CAP so the message stays readable; the rest are one tap away
     on the dashboard's Today tab.
  4. STREAK WATCH — signals on a 3+ day up-streak, risky or not, capped at
     WATCH_CAP. Visibility, not a buy signal — includes extended entries
     the shortlist deliberately excludes. Judge the extension yourself.
  5. Everything else collapsed to one line. "Nothing needs you" is a feature.

Mobile formatting: each stock is two SHORT lines (ticker+gain, then the
details), never one long line. No backticks around tickers — backticks
force an inline monospace font that mixes badly with the surrounding
proportional text and causes unpredictable mid-word wraps on a phone.

Reads data.json (written by export_dashboard.py earlier in the same workflow)
so it sees the same enriched fields the dashboard does.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
FLAG = {"US": "\U0001F1FA\U0001F1F8", "IN": "\U0001F1EE\U0001F1F3"}
TIER = [(30, "trail 7% below high"), (20, "trail 10% below high"),
        (10, "breakeven locked"), (0, "initial stop")]


def send(text: str):
    if not (TOKEN and CHAT):
        print("digest (telegram disabled):\n" + text)
        return
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT, "text": text, "parse_mode": "Markdown",
              "disable_web_page_preview": True},
        timeout=15,
    )


def _tier(gain_pct: float) -> str:
    for cut, label in TIER:
        if gain_pct >= cut:
            return label
    return "initial stop"


def _pick_lines(key, cur, s, tag=""):
    """Two short lines per stock — never one long line that can wrap badly."""
    gain = s.get("gain_pct", 0)
    line1 = f"{FLAG[key]} *{s['ticker']}*  {gain:+.1f}%{tag}"
    line2 = (f"   streak {s.get('streak')} \u00b7 risk {s.get('entry_risk_now'):.1f}%"
              f" \u00b7 stop {cur}{s.get('entry_stop_now')}")
    return [line1, line2]


def build() -> str:
    now = datetime.now(timezone.utc)
    lines = [f"*ASR \u2014 {now.strftime('%a %b %d')}*"]

    data_path = Path(__file__).parent / "data.json"
    if not data_path.exists():
        return "\n".join(lines + ["\ndata.json missing \u2014 run export_dashboard.py first."])
    data = json.loads(data_path.read_text())

    # ---- 1) HOLDING: verdict, two short lines per position ------------
    hold_lines = []
    for key, m in data["markets"].items():
        cur = m["currency"]
        for p in m.get("positions", []):
            if p.get("status") != "open":
                continue
            stop = p.get("current_stop") or p.get("initial_stop")
            now_p, pl = p.get("now_price"), p.get("pl_pct")
            if now_p and stop:
                dist = (now_p - stop) / now_p * 100
                verdict = ("\u26a0\ufe0f near stop" if dist <= 2 else "HOLD")
                hold_lines.append(f"\U0001F7E2 *{p['ticker']}*  {pl:+.1f}%")
                hold_lines.append(
                    f"   stop {cur}{stop} ({_tier(pl or 0)}) \u00b7 "
                    f"{dist:.1f}% clear \u2192 {verdict}")
            else:
                hold_lines.append(f"\U0001F7E2 *{p['ticker']}*  \u2014 stop {cur}{stop}")
    if hold_lines:
        lines.append("\n*HOLDING*")
        lines.extend(hold_lines)

    # ---- 2) TODAY'S SHORTLIST, split NEW vs carried-over --------------
    CARRY_CAP = 6
    total_signals = 0
    all_short = []
    for key, m in data["markets"].items():
        cur = m["currency"]
        sigs = m.get("signals", [])
        total_signals += len(sigs)
        picks = [s for s in sigs
                 if s.get("status") == "TRENDING"
                 and (s.get("streak") or 0) >= 2
                 and s.get("clean_entry") is True]
        for s in picks:
            all_short.append((key, cur, s))

    new_short = sorted([t for t in all_short if (t[2].get("age_days") or 0) == 0],
                        key=lambda t: t[2].get("entry_risk_now") or 99)
    carried = sorted([t for t in all_short if (t[2].get("age_days") or 0) > 0],
                      key=lambda t: t[2].get("entry_risk_now") or 99)

    short = []
    if new_short:
        lines.append(f"\n\U0001F195 *NEW TODAY* ({len(new_short)})")
        for i, (key, cur, s) in enumerate(new_short):
            tag = "  \u2190 cleanest" if i == 0 and len(new_short) > 1 else ""
            lines.extend(_pick_lines(key, cur, s, tag))
            short.append(s)

    if carried:
        shown = carried[:CARRY_CAP]
        lines.append(f"\n\U0001F3AF *STILL QUALIFYING* ({len(carried)})")
        for key, cur, s in shown:
            lines.extend(_pick_lines(key, cur, s))
            short.append(s)
        if len(carried) > CARRY_CAP:
            lines.append(f"   +{len(carried) - CARRY_CAP} more on the dashboard's Today tab")

    if not new_short and not carried:
        lines.append("\n\U0001F3AF *TODAY'S SHORTLIST* \u2014 none")

    # ---- 3) STREAK WATCH: every 3+ day up-streak, risky or not --------
    STREAK_MIN = 3
    WATCH_CAP = 8
    shortlist_tickers = {s["ticker"] for s in short}
    watch_all = []
    for key, m in data["markets"].items():
        cur = m["currency"]
        picks = [s for s in m.get("signals", [])
                 if (s.get("streak") or 0) >= STREAK_MIN
                 and s["ticker"] not in shortlist_tickers]
        for s in picks:
            watch_all.append((key, cur, s))
    watch_all.sort(key=lambda t: t[2].get("streak") or 0, reverse=True)
    watch = watch_all[:WATCH_CAP]

    if watch:
        lines.append(f"\n\U0001F525 *STREAK WATCH* ({STREAK_MIN}+ days)")
        for key, cur, s in watch:
            risk = s.get("entry_risk_now")
            risk_txt = f"risk {risk:.1f}%" if (risk is not None and s.get("clean_entry")) else "extended"
            gain = s.get("gain_pct", 0)
            lines.append(f"{FLAG[key]} *{s['ticker']}*  {gain:+.1f}%")
            lines.append(f"   streak {s.get('streak')}d \u00b7 {risk_txt}")
        if len(watch_all) > WATCH_CAP:
            lines.append(f"   +{len(watch_all) - WATCH_CAP} more on the dashboard")

    # ---- 4) Everything else, collapsed --------------------------------
    rest = total_signals - len(short) - len(watch_all)
    if rest > 0:
        lines.append(f"\n\U0001F634 Everything else: {rest} signals, nothing actionable")

    lines.append("\n_One decision max. Stops are the plan._")
    return "\n".join(lines)


if __name__ == "__main__":
    send(build())
