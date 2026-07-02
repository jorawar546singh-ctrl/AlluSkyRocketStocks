"""
Telegram EOD digest, v3 — decisions, not data.

Format goal: tell me what to DO, then let me close the app.
  1. HOLDING — position(s) first, with a verdict: distance to stop + tier.
  2. TODAY'S SHORTLIST — signals that are actionable *right now*, mechanically:
       status TRENDING + streak >= 2 + clean entry (risk-to-stop <= 12%).
     Same rule as the dashboard's "Today" tab. Cleanest (lowest risk) first.
  3. Everything else collapsed to one line. "Nothing needs you" is a feature.

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


def build() -> str:
    now = datetime.now(timezone.utc)
    lines = [f"*ASR — {now.strftime('%a %b %d')}*"]

    data_path = Path(__file__).parent / "data.json"
    if not data_path.exists():
        return "\n".join(lines + ["\ndata.json missing — run export_dashboard.py first."])
    data = json.loads(data_path.read_text())

    # ---- 1) HOLDING: verdict line per open position -------------------
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
                hold_lines.append(
                    f"\U0001F7E2 `{p['ticker']}` {pl:+.1f}% | stop {cur}{stop}"
                    f" ({_tier(pl or 0)}) | {dist:.1f}% above stop \u2192 {verdict}")
            else:
                hold_lines.append(f"\U0001F7E2 `{p['ticker']}` — stop {cur}{stop}")
    if hold_lines:
        lines.append("\n*HOLDING*")
        lines.extend(hold_lines)

    # ---- 2) TODAY'S SHORTLIST (same rule as dashboard Today tab) ------
    total_signals = 0
    short = []
    for key, m in data["markets"].items():
        cur = m["currency"]
        sigs = m.get("signals", [])
        total_signals += len(sigs)
        picks = [s for s in sigs
                 if s.get("status") == "TRENDING"
                 and (s.get("streak") or 0) >= 2
                 and s.get("clean_entry") is True]
        picks.sort(key=lambda s: s.get("entry_risk_now") or 99)
        for i, s in enumerate(picks):
            tag = " \u2190 cleanest" if i == 0 and len(picks) > 1 else ""
            short.append(
                f"{FLAG[key]} `{s['ticker']}` {s.get('gain_pct', 0):+.1f}%"
                f"  streak {s.get('streak')}  risk {s.get('entry_risk_now'):.1f}%"
                f"  stop {cur}{s.get('entry_stop_now')}{tag}")

    if short:
        lines.append(f"\n\U0001F3AF *TODAY'S SHORTLIST* ({len(short)})")
        lines.extend(short)
    else:
        lines.append("\n\U0001F3AF *TODAY'S SHORTLIST* — none")

    # ---- 3) Everything else, collapsed --------------------------------
    rest = total_signals - len(short)
    if rest > 0:
        lines.append(f"\n\U0001F634 Everything else: {rest} signals, nothing actionable")

    lines.append("\n_One decision max. Stops are the plan._")
    return "\n".join(lines)


if __name__ == "__main__":
    send(build())
