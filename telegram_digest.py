"""
Telegram EOD digest, v2.1 — glanceable.

Format goal: readable in 5 seconds, half asleep.
  - New breakouts per market, primary first (★), price + stop + RS only
  - Open positions with stops
  - Nothing else. No grades, no tiers, no metrics soup.
"""
import os
from datetime import datetime, timezone

import requests

from core.config import MARKETS
from core.db import connect

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
FLAG = {"US": "\U0001F1FA\U0001F1F8", "IN": "\U0001F1EE\U0001F1F3"}


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


def build() -> str:
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    lines = [f"*ASR — {now.strftime('%a %b %d')}*"]

    any_new = False
    with connect() as con:
        for key, cfg in MARKETS.items():
            sigs = con.execute(
                "SELECT * FROM signals WHERE market=? AND scan_date=? "
                "ORDER BY rs_pct DESC LIMIT ?", (key, today, cfg.top_n)).fetchall()
            if sigs:
                any_new = True
                lines.append(f"\n{FLAG[key]} *{key}* — {len(sigs)} new")
                for i, s in enumerate(sigs):
                    mark = "\u2605" if i == 0 else "\u00b7"
                    rs = f" (RS {s['rs_pct']:.0f})" if s["rs_pct"] is not None else ""
                    lines.append(f"{mark} `{s['ticker']}` {cfg.currency}{s['price']}"
                                 f" — stop {cfg.currency}{s['suggested_stop']}{rs}")
        if not any_new:
            lines.append("\nNo new breakouts. Nothing to do today.")

        pos_lines = []
        for key, cfg in MARKETS.items():
            for p in con.execute(
                    "SELECT * FROM positions WHERE market=? AND status='open'", (key,)):
                stop = p["current_stop"] or p["initial_stop"]
                pos_lines.append(f"\u00b7 `{p['ticker']}` — stop {cfg.currency}{stop}")
        if pos_lines:
            lines.append("\n*Holding*")
            lines.extend(pos_lines)

    lines.append("\n_Dashboard for details. Stops are the plan._")
    return "\n".join(lines)


if __name__ == "__main__":
    send(build())
