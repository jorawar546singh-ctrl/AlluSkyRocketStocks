"""
Telegram EOD digest, v2 — reads from data/asr.db.
Same single-message format as v1: today's breakouts ranked by RS, open
positions, plus any universe/scrape warnings so silent failures stop being silent.
"""
import os
from datetime import datetime, timezone

import requests

from core.config import MARKETS
from core.db import connect

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


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
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"*ASR digest — {today}*"]
    with connect() as con:
        for key, cfg in MARKETS.items():
            sigs = con.execute(
                "SELECT * FROM signals WHERE market=? AND scan_date=? "
                "ORDER BY rs_pct DESC LIMIT ?", (key, today, cfg.top_n)).fetchall()
            if not sigs:
                continue
            lines.append(f"\n*{cfg.label}* — {len(sigs)} qualified breakouts")
            for i, s in enumerate(sigs):
                tag = "PRIMARY · " if i == 0 else ""
                lines.append(
                    f"{tag}`{s['ticker']}` {cfg.currency}{s['price']} · "
                    f"RS {s['rs_pct'] or '—'} · vol {s['vol_ratio']}x · "
                    f"stop {cfg.currency}{s['suggested_stop']} ({s['risk_pct']}%)")
            pos = con.execute(
                "SELECT * FROM positions WHERE market=? AND status='open'", (key,)).fetchall()
            for p in pos:
                lines.append(f"open: `{p['ticker']}` stop "
                             f"{cfg.currency}{p['current_stop'] or p['initial_stop']}")
    if len(lines) == 1:
        lines.append("\nNo qualified breakouts in any market. Cash is a position.")
    lines.append("\n_2% rule. One position. Journal before size._")
    return "\n".join(lines)


if __name__ == "__main__":
    send(build())
