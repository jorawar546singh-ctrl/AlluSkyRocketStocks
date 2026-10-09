"""
Pull tickers somebody else mentioned into the db.

Two sources, same pipeline:

    python ingest_mentions.py discord US --channel 123456789 --hours 24
    python ingest_mentions.py file US --path watchlist.txt

The file mode is not a toy fallback -- it is how this runs before any Discord
plumbing exists, and how it keeps running if the channel goes away. Anything
with tickers in it works: a pasted message, a list, one symbol per line.

DISCORD ACCESS
--------------
Reading a channel needs a BOT token (DISCORD_BOT_TOKEN), and a bot has to be
invited to the server by someone with Manage Server. Automating a personal
account with a user token instead is forbidden by Discord and risks account
termination -- this script will not accept one, and that is deliberate.

If the source channel belongs to someone else: if it is an Announcement channel
(megaphone icon), follow it into a server you own and point this at the mirror.
Following needs no permission in their server, only Manage Webhooks in yours.
"""
import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

from core import tickers as tk
from core.config import MARKETS
from core.datafeed import fetch_history
from core.db import connect

API = "https://discord.com/api/v10"
CONTEXT_CHARS = 280


def _fetch_discord(channel_id: str, token: str, hours: int) -> list[dict]:
    """Recent messages from one channel, newest first, paged back `hours`."""
    if not token.strip():
        sys.exit("DISCORD_BOT_TOKEN is empty")
    # A bot token is presented as "Bot <token>". A raw user token would be sent
    # bare -- refusing to build that header is the guardrail, not a style choice.
    headers = {"Authorization": f"Bot {token.strip()}",
               "User-Agent": "AlluSkyRocketStocks (github, v2)"}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out, before = [], None

    while True:
        params = {"limit": 100}
        if before:
            params["before"] = before
        r = requests.get(f"{API}/channels/{channel_id}/messages",
                         headers=headers, params=params, timeout=30)
        if r.status_code == 401:
            sys.exit("Discord rejected the token (401). Check DISCORD_BOT_TOKEN.")
        if r.status_code == 403:
            sys.exit("Discord returned 403 — the bot is not in that server, or "
                     "lacks View Channel / Read Message History on it.")
        if r.status_code == 404:
            sys.exit(f"Channel {channel_id} not found — check the id.")
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for m in batch:
            ts = datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00"))
            if ts < cutoff:
                return out
            out.append({"id": m["id"], "ts": ts,
                        "text": " ".join(filter(None, [
                            m.get("content", ""),
                            # Alert bots often put the ticker in an embed title
                            # rather than the message body.
                            *[e.get("title", "") for e in m.get("embeds", [])],
                            *[e.get("description", "") for e in m.get("embeds", [])],
                        ]))})
        before = batch[-1]["id"]
        if len(batch) < 100:
            break
    return out


def _read_file(path: str) -> list[dict]:
    with open(path) as f:
        text = f.read()
    if not text.strip():
        sys.exit(f"{path} is empty")
    now = datetime.now(timezone.utc)
    # One pseudo-message per line keeps context readable; a blob works too.
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return [{"id": None, "ts": now, "text": ln} for ln in lines]


def run(mode: str, market_key: str, channel: str | None, path: str | None,
        hours: int) -> int:
    cfg = MARKETS[market_key]
    if mode == "discord":
        token = os.environ.get("DISCORD_BOT_TOKEN", "")
        if not token:
            sys.exit("DISCORD_BOT_TOKEN not set. In Actions, add it as a repo secret.")
        msgs = _fetch_discord(channel, token, hours)
        src, chan = "discord", channel
    else:
        msgs = _read_file(path)
        src, chan = "manual", os.path.basename(path)
    print(f"  {src}: {len(msgs)} message(s) to scan")

    # Candidates first, priced once as a batch -- validation is the expensive
    # step and the same symbol usually appears in several messages.
    candidates: dict[str, dict] = {}
    for m in msgs:
        for sym, how in tk.extract(m["text"]).items():
            prev = candidates.get(sym)
            # Keep the EARLIEST mention: the tip's value is when it was made.
            if prev is None or m["ts"] < prev["ts"]:
                candidates[sym] = {"ts": m["ts"], "id": m["id"], "how": how,
                                   "text": m["text"][:CONTEXT_CHARS]}
            elif how == "cashtag":
                prev["how"] = "cashtag"
    if not candidates:
        print("  no ticker candidates found")
        return 0
    print(f"  {len(candidates)} candidate(s): {', '.join(sorted(candidates))}")

    priced = tk.validate(list(candidates), cfg, fetch_history)
    rejected = sorted(set(candidates) - set(priced))
    if rejected:
        print(f"  dropped {len(rejected)} (not tradable in {cfg.key}"
              f" or outside {cfg.currency}{cfg.min_price}-{cfg.max_price}): "
              f"{', '.join(rejected)}")
    if not priced:
        return 0

    saved = 0
    with connect() as con:
        for sym, price in priced.items():
            c = candidates[sym]
            # SQLite treats NULLs as distinct in a UNIQUE constraint, so a
            # manual entry with message_id NULL never conflicts with itself and
            # every re-run would duplicate the file. Synthesise a stable id:
            # same file + same day + same ticker is the same mention, while the
            # same ticker tomorrow is legitimately a fresh tip.
            if c["id"] is None:
                c["id"] = f"{chan}:{c['ts'].strftime('%Y-%m-%d')}"
            cur = con.execute(
                "INSERT INTO mentions (market,ticker,mention_ts,mention_date,source,"
                "channel,message_id,how,context,price_at_mention) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source,message_id,ticker) DO NOTHING",
                (cfg.key, sym, c["ts"].isoformat(timespec="seconds"),
                 c["ts"].strftime("%Y-%m-%d"), src, chan, c["id"], c["how"],
                 c["text"], price))
            saved += cur.rowcount if cur.rowcount > 0 else 0
    print(f"  saved {saved} new mention(s) "
          f"({len(priced) - saved} already recorded)\n")
    return saved


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["discord", "file"])
    p.add_argument("market", nargs="?", default="US", choices=list(MARKETS))
    p.add_argument("--channel", help="Discord channel id (discord mode)")
    p.add_argument("--path", help="text file to read (file mode)")
    p.add_argument("--hours", type=int, default=24,
                   help="how far back to read, discord mode (default 24)")
    a = p.parse_args()
    if a.mode == "discord" and not a.channel:
        p.error("--channel is required in discord mode")
    if a.mode == "file" and not a.path:
        p.error("--path is required in file mode")
    run(a.mode, a.market, a.channel, a.path, a.hours)
