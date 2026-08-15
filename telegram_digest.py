"""
Telegram EOD digest, v8 — grouped by market, one clean top-5, bold numbers.

Format goal: tell me what to DO, then let me close the app — and let me
jump straight to the ONE market I actually care about without scanning
past the others.

Top-level structure is MARKET FIRST, not category-first:
  🇺🇸 US
    HOLDING (if any open position)
    NEW TODAY (signals that fired today — always shown in full, no cap)
    TOP 5 ON STREAK (status TRENDING + streak>=2, ranked by streak, capped
                      at 5 — replaces the old separate "still qualifying"
                      / "streak watch" sections, which overlapped and were
                      confusing as two similar-looking lists)
  🇮🇳 INDIA
    ... same structure ...
  🇨🇦 CANADA
    ... same structure, or "no signals tracked yet" while it's new ...

Readability note: Telegram's Bot API has NO way to control font size in a
message — that's entirely the recipient's own app-wide setting (Settings ->
Appearance -> Font Size), not something a bot can set per-message. What IS
controllable: bolding the actual numbers (not the labels) so they stand out
from the surrounding plain text at a glance, which is what changed here.

Each market is a self-contained block — no interleaving of US/India/Canada
picks in one flat list.

A "\u26a1 LIVE" tag means the signal came from the mid-session scan: the
close isn't confirmed yet and it may be replaced or removed after 4pm ET.

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
TOP_N = 5  # top-N-on-streak cap per market


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
    """Two short lines per stock. Numbers are bold, labels are plain —
    that's the only real "make it easier to read" lever Telegram gives a
    bot; actual font size is the user's own app setting, not ours to set."""
    gain = s.get("gain_pct", 0)
    risk = s.get("entry_risk_now")
    risk_txt = f"*{risk:.1f}%*" if (risk is not None and s.get("clean_entry")) else "*extended*"
    live = " \u26a1 LIVE" if s.get("source") == "scanner_intraday" else ""
    line1 = f"  *{s['ticker']}*{live}  *{gain:+.1f}%*{tag}"
    line2 = f"     streak *{s.get('streak')}* \u00b7 risk {risk_txt} \u00b7 stop *{cur}{s.get('entry_stop_now')}*"
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
            hold.append(f"  \U0001F7E2 *{p['ticker']}*  *{pl:+.1f}%*")
            hold.append(f"     stop *{cur}{stop}* ({_tier(pl or 0)}) \u00b7 *{dist:.1f}%* clear \u2192 {verdict}")
        else:
            hold.append(f"  \U0001F7E2 *{p['ticker']}*  \u2014 stop *{cur}{stop}*")
    if hold:
        out.append(" HOLDING")
        out.extend(hold)

    if not sigs:
        out.append(" _no signals tracked yet_")
        return out

    # ---- NEW TODAY: always shown in full, no cap ----
    new_today = sorted(
        [s for s in sigs if (s.get("age_days") or 0) == 0
         and s.get("status") == "TRENDING" and (s.get("streak") or 0) >= 1],
        key=lambda s: s.get("entry_risk_now") or 99)
    used = set()
    if new_today:
        out.append(f" \U0001F195 NEW TODAY ({len(new_today)})")
        for i, s in enumerate(new_today):
            tag = "  \u2190 cleanest" if i == 0 and len(new_today) > 1 else ""
            out.extend(_pick_lines(cur, s, tag))
            used.add(s["ticker"])

    # ---- TOP 5 ON STREAK: TRENDING + streak>=2, ranked by streak,
    #      excludes anything already shown above in NEW TODAY ----
    candidates = [s for s in sigs
                  if s.get("status") == "TRENDING"
                  and (s.get("streak") or 0) >= 2
                  and s["ticker"] not in used]
    candidates.sort(key=lambda s: (-(s.get("streak") or 0), s.get("entry_risk_now") or 99))
    top5 = candidates[:TOP_N]
    if top5:
        out.append(f" \U0001F525 TOP {len(top5)} ON STREAK")
        for s in top5:
            out.extend(_pick_lines(cur, s))
            used.add(s["ticker"])
        if len(candidates) > TOP_N:
            out.append(f"   +{len(candidates) - TOP_N} more on the dashboard's Today tab")

    rest = len(sigs) - len(used)
    if not new_today and not top5:
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
