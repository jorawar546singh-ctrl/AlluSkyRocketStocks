#!/usr/bin/env python3
"""
Fill sector / industry / company_name for signals that have none.

Historical US rows predate the per-signal company lookup, so ~555 of them
carry a null sector. The scan itself reads sector off the universe page for
free, but that only helps signals fired from that day forward -- the old rows
need one Finviz request each.

    python3 backfill_company.py                 # US, all missing
    python3 backfill_company.py --market US --limit 100
    python3 backfill_company.py --dry-run

Throttled and resumable by design. One request every DELAY seconds, in a
stable order, and each result is committed as it arrives -- so an interrupted
run loses nothing and re-running simply continues where it stopped. Tickers
are de-duplicated first: the same symbol can appear in dozens of signals and
its sector does not change between them, so it is fetched once and applied to
every row that shares it.

Only null fields are written (COALESCE), so this can never overwrite a value
the scan already captured.
"""
import argparse
import sys
import time

from core import company as comp
from core.config import MARKETS
from core.db import connect

DELAY = 1.5          # seconds between requests -- polite, and ~14min for 555
MAX_CONSECUTIVE_FAILURES = 12


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="US", help="market key (default US)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N tickers")
    ap.add_argument("--delay", type=float, default=DELAY)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.market not in MARKETS:
        print(f"unknown market {a.market}; known: {list(MARKETS)}")
        return 2
    if a.market != "US":
        print(f"note: the lookup only covers US names; {a.market} will return nulls")

    with connect() as con:
        rows = con.execute(
            "SELECT ticker, COUNT(*) n FROM signals "
            "WHERE market=? AND (sector IS NULL OR industry IS NULL "
            "                    OR company_name IS NULL) "
            "GROUP BY ticker ORDER BY n DESC, ticker", (a.market,)).fetchall()
    todo = [(r["ticker"], r["n"]) for r in rows]
    if a.limit:
        todo = todo[:a.limit]

    affected = sum(n for _, n in todo)
    print(f"{a.market}: {len(todo)} distinct tickers missing company data, "
          f"covering {affected} signal rows")
    if not todo:
        return 0
    print(f"  ~{len(todo) * a.delay / 60:.0f} min at {a.delay}s between requests")
    if a.dry_run:
        for t, n in todo[:25]:
            print(f"    {t:<10} {n} row(s)")
        print("  (dry run — nothing fetched or written)")
        return 0

    filled = blank = 0
    consecutive = 0
    for i, (t, n) in enumerate(todo, 1):
        info = comp.lookup(t, a.market)
        got = {k: v for k, v in info.items() if v}
        if got:
            consecutive = 0
            filled += 1
            # Committed per ticker, so an interrupt keeps everything so far.
            with connect() as con:
                con.execute(
                    "UPDATE signals SET sector=COALESCE(sector,?), "
                    "industry=COALESCE(industry,?), "
                    "company_name=COALESCE(company_name,?), "
                    "next_earnings_date=COALESCE(next_earnings_date,?) "
                    "WHERE market=? AND ticker=?",
                    (info.get("sector"), info.get("industry"),
                     info.get("company_name"), info.get("next_earnings_date"),
                     a.market, t))
            print(f"  [{i}/{len(todo)}] {t:<10} {got.get('sector','?')} / "
                  f"{got.get('industry','?')}  ({n} row(s))")
        else:
            blank += 1
            consecutive += 1
            print(f"  [{i}/{len(todo)}] {t:<10} nothing returned")
            # A long unbroken run of blanks means rate-limiting or a markup
            # change, not 12 obscure tickers. Stop rather than hammer the
            # source for another 500 requests that will also come back empty.
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                print(f"\n  {consecutive} blanks in a row — stopping. Either the "
                      f"source is rate-limiting or its markup moved; re-run later "
                      f"and it will resume from here.")
                break
        if i < len(todo):
            time.sleep(a.delay)

    print(f"\nfilled {filled}, blank {blank}")
    with connect() as con:
        left = con.execute(
            "SELECT COUNT(*) FROM signals WHERE market=? AND sector IS NULL",
            (a.market,)).fetchone()[0]
    print(f"{a.market} signals still missing a sector: {left}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
