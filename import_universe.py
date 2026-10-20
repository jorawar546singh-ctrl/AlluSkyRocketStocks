#!/usr/bin/env python3
"""
Seed the universe cache from a Finviz screener export.

Why this exists: Finviz blocks the GitHub Actions datacenter IP on
screener.ashx (quote.ashx still works, which is why company lookups kept
running while the universe collapsed to 48 tickers). No amount of header
tuning fixes an IP block, and the scanner cannot find new breakouts while it
is looking at 12% of the market.

A browser on a home connection is not blocked, and Finviz now offers CSV
export on the free tier. So: export there, import here. The list lands in
universe_cache and every scan uses it until a live source works again.

    1. finviz.com/screener.ashx with your filters
    2. Export -> save the .csv
    3. python3 import_universe.py ~/Downloads/finviz.csv
    4. commit data/asr.db and push

Price and volume bounds come from the market config, so an export taken with
looser filters is narrowed here rather than silently widening the universe.
"""
import argparse
import csv
import json
import sys
from datetime import datetime, timezone

from core.config import MARKETS
from core.db import connect

# Finviz has renamed these across versions; accept any of them.
TICKER_COLS = ("Ticker", "Symbol", "ticker", "symbol")
PRICE_COLS = ("Price", "price", "Last", "Close")
VOL_COLS = ("Volume", "volume", "Average Volume", "Avg Volume")


def _pick(row: dict, names) -> str | None:
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return None


def _num(v) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("$", "").replace("%", "")
    mult = 1.0
    if s[-1:].upper() in ("K", "M", "B"):
        mult = {"K": 1e3, "M": 1e6, "B": 1e9}[s[-1].upper()]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--market", default="US")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap the list (0 = the market's MAX_CANDIDATES)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.market not in MARKETS:
        print(f"unknown market {a.market}; known: {list(MARKETS)}")
        return 2
    cfg = MARKETS[a.market]

    with open(a.csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("empty CSV")
        return 1
    print(f"read {len(rows)} rows from {a.csv_path}")
    print(f"columns: {list(rows[0].keys())[:10]}")

    kept, dropped_price, dropped_vol, no_ticker = [], 0, 0, 0
    seen = set()
    for r in rows:
        t = _pick(r, TICKER_COLS)
        if not t:
            no_ticker += 1
            continue
        t = t.strip().upper()
        if not t or t in seen:
            continue
        price = _num(_pick(r, PRICE_COLS))
        vol = _num(_pick(r, VOL_COLS))
        # Bounds from config, so a looser export cannot widen the universe.
        if price is not None and not (cfg.min_price <= price <= cfg.max_price):
            dropped_price += 1
            continue
        if vol is not None and vol < 500_000:
            dropped_vol += 1
            continue
        seen.add(t)
        kept.append(t)

    cap = a.limit or getattr(cfg, "max_candidates", 400)
    final = kept[:cap]
    print(f"  dropped {dropped_price} outside ${cfg.min_price}-${cfg.max_price}, "
          f"{dropped_vol} under 500K volume, {no_ticker} with no ticker column")
    print(f"  keeping {len(final)} of {len(kept)} eligible (cap {cap})")
    print(f"  sample: {', '.join(final[:12])}")
    if a.dry_run:
        print("  (dry run — nothing written)")
        return 0
    if len(final) < 50:
        print("  REFUSING to write: fewer than 50 tickers is not a usable universe.")
        print("  Check the export's columns, or widen the screener filters.")
        return 1

    with connect() as con:
        con.execute(
            "INSERT INTO universe_cache (market, saved_at, n, tickers) VALUES (?,?,?,?) "
            "ON CONFLICT(market) DO UPDATE SET saved_at=excluded.saved_at, "
            "n=excluded.n, tickers=excluded.tickers",
            (a.market, datetime.now(timezone.utc).strftime("%Y-%m-%d"),
             len(final), json.dumps(final)))
    print(f"\nwrote {len(final)} tickers to universe_cache[{a.market}]")
    print("commit data/asr.db and push; the next scan will use it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
