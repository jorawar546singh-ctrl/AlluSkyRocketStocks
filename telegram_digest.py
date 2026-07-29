"""
Telegram EOD digest, v7 — grouped by market, decisions not data.

Format goal: tell me what to DO, then let me close the app — and let me
jump straight to the ONE market I actually care about without scanning
past the others.

Top-level structure is MARKET FIRST, not category-first:
  🇺🇸 US
    HOLDING (if any open position)
    NEW TODAY (signals that fired today — always shown in full)
    STILL QUALIFYING (older shortlist signals, capped)
    STREAK WATCH (3+ day streaks, risky or not, capped)
  🇮🇳 INDIA
    ... same structure ...
  🇨🇦 CANADA
    ... same structure, or "no signals tracked yet" while it's new ...

Each market is a self-contained block — no interleaving of US/India/Canada
picks in one flat list, which is what made earlier versions hard to read
at a glance. If you only trade one market, skip straight to its section.

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
FLAG = {"US": "\U0001F1FA\U0001F1F8", "IN": "\U0001F1EE\U0001F1F3", "CA": "\U0001F1E8\U0001F1E6"}
MARKET_ORDER = ["US", "IN", "CA"]  # fixed order, matches the dashboard tabs
TIER = [(30, "trail 7% below high"), (20, "trail 10% below high"),
        (10, "breakeven locked"), (0, "initial stop")]
CARRY_CAP = 4   # per-market cap on "still qualifying" (was pooled across markets before)
WATCH_CAP = 5   # per-market cap on "streak watch"


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


def _pick_lines(cur, s, tag=""):
    """Two short lines per stock — never one long line that can wrap badly."""
    gain = s.get("gain_pct", 0)
    line1 = f"  *{s['ticker']}*  {gain:+.1f}%{tag}"
    line2 = (f"     streak {s.get('streak')} \u00b7 risk {s.get('entry_risk_now'):.1f}%"
              f" \u00b7 stop {cur}{s.get('entry_stop_now')}")
    return [line1, line2]


def _market_block(key: str, m: dict) -> list[str]:
    cur = m["currency"]
    label = m.get("label", key)
    sigs = m.get("signals", [])
    out = [f"\n{FLAG.get(key,'')} *{label.upper()}*"]

    # ---- HOLDING ----
    hold = []
    for p in m.get("positions", []):
        if p.get("status") != "open":
            continue
        stop = p.get("current_stop") or p.get("initial_stop")
        now_p, pl = p.get("now_price"), p.get("pl_pct")
        if now_p and stop:
            dist = (now_p - stop) / now_p * 100
            verdict = "\u26a0\ufe0f near stop" if dist <= 2 else "HOLD"
            hold.append(f"  \U0001F7E2 *{p['ticker']}*  {pl:+.1f}%")
            hold.append(f"     stop {cur}{stop} ({_tier(pl or 0)}) \u00b7 {dist:.1f}% clear \u2192 {verdict}")
        else:
            hold.append(f"  \U0001F7E2 *{p['ticker']}*  \u2014 stop {cur}{stop}")
    if hold:
        out.append(" HOLDING")
        out.extend(hold)

    if not sigs:
        out.append(" _no signals tracked yet_")
        return out

    # ---- shortlist: TRENDING + streak>=2 + clean entry ----
    picks = [s for s in sigs
             if s.get("status") == "TRENDING"
             and (s.get("streak") or 0) >= 2
             and s.get("clean_entry") is True]
    new_short = sorted([s for s in picks if (s.get("age_days") or 0) == 0],
                        key=lambda s: s.get("entry_risk_now") or 99)
    carried = sorted([s for s in picks if (s.get("age_days") or 0) > 0],
                      key=lambda s: s.get("entry_risk_now") or 99)
    used_tickers = set()

    if new_short:
        out.append(f" \U0001F195 NEW TODAY ({len(new_short)})")
        for i, s in enumerate(new_short):
            tag = "  \u2190 cleanest" if i == 0 and len(new_short) > 1 else ""
            out.extend(_pick_lines(cur, s, tag))
            used_tickers.add(s["ticker"])

    if carried:
        shown = carried[:CARRY_CAP]
        out.append(f" \U0001F3AF STILL QUALIFYING ({len(carried)})")
        for s in shown:
            out.extend(_pick_lines(cur, s))
            used_tickers.add(s["ticker"])
        if len(carried) > CARRY_CAP:
            out.append(f"   +{len(carried) - CARRY_CAP} more on the dashboard's Today tab")

    # ---- streak watch: 3+ day streaks, risky or not, excluding shortlist ----
    watch_all = [s for s in sigs
                 if (s.get("streak") or 0) >= 3
                 and s["ticker"] not in used_tickers]
    watch_all.sort(key=lambda s: s.get("streak") or 0, reverse=True)
    watch = watch_all[:WATCH_CAP]
    if watch:
        out.append(f" \U0001F525 STREAK WATCH (3+ days)")
        for s in watch:
            risk = s.get("entry_risk_now")
            risk_txt = f"risk {risk:.1f}%" if (risk is not None and s.get("clean_entry")) else "extended"
            gain = s.get("gain_pct", 0)
            out.append(f"  *{s['ticker']}*  {gain:+.1f}%")
            out.append(f"     streak {s.get('streak')}d \u00b7 {risk_txt}")
        if len(watch_all) > WATCH_CAP:
            out.append(f"   +{len(watch_all) - WATCH_CAP} more on the dashboard")

    rest = len(sigs) - len(used_tickers) - len(watch)
    if not new_short and not carried and not watch:
        out.append(f" \U0001F634 nothing actionable ({len(sigs)} tracked)")
    elif rest > 0:
        out.append(f" \U0001F634 +{rest} more tracked, nothing actionable")

    return out


def build() -> str:
    now = datetime.now(timezone.utc)
    lines = [f"*ASR \u2014 {now.strftime('%a %b %d')}*"]

    data_path = Path(__file__).parent / "data.json"
    if not data_path.exists():
        return "\n".join(lines + ["\ndata.json missing \u2014 run export_dashboard.py first."])
    data = json.loads(data_path.read_text())
    markets = data.get("markets", {})

    for key in MARKET_ORDER:
        if key in markets:
            lines.extend(_market_block(key, markets[key]))

    lines.append("\n_One decision max. Stops are the plan._")
    return "\n".join(lines)


if __name__ == "__main__":
    send(build())
