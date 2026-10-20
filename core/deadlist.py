"""
Tickers the feed has stopped answering for.

A delisted or renamed symbol never leaves the signal history, so it stays in
the fetch list forever. Every run then pays the full retry budget on it -- the
bulk download retries three times, then the per-ticker fallback twice more,
in both the scan and the export -- to confirm again that it is gone. CPRX and
SKLZ have been doing exactly that since April.

So: count consecutive failures, and once a ticker has failed enough times in a
row, stop asking. Three rules keep this from quietly shrinking the universe:

  * a single success clears the record completely, so a one-day feed outage
    cannot bury a live ticker;
  * a skipped ticker is retried after RETRY_AFTER_DAYS, because symbols do come
    back -- a renamed listing, a feed fixing its own data;
  * nothing is ever deleted from `signals`. The history of what the scanner
    found stays intact; only the daily fetching stops.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.db import connect

MAX_FAILS = 3           # consecutive misses before a ticker is rested
RETRY_AFTER_DAYS = 30   # ...and how long before it gets another chance


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def skiplist(market: str) -> set[str]:
    """Tickers to leave out of the next fetch."""
    try:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=RETRY_AFTER_DAYS)).strftime("%Y-%m-%d")
        with connect() as con:
            return {r[0] for r in con.execute(
                "SELECT ticker FROM dead_tickers WHERE market=? AND fails>=? "
                "AND last_failed > ?", (market, MAX_FAILS, cutoff))}
    except Exception:                                          # noqa: BLE001
        return set()        # never let bookkeeping cost a scan


def filter_live(market: str, tickers: list[str], label: str = "") -> list[str]:
    dead = skiplist(market)
    if not dead:
        return list(tickers)
    keep = [t for t in tickers if t.split(".")[0] not in dead]
    n = len(tickers) - len(keep)
    if n:
        print(f"  datafeed: skipping {n} ticker(s) the feed has no data for"
              f"{f' ({label})' if label else ''}: "
              f"{', '.join(sorted(dead)[:8])}{'…' if len(dead) > 8 else ''}")
    return keep


def record(market: str, requested: list[str], found: set[str]) -> None:
    """Bump the miss count for what did not come back; clear it for what did."""
    try:
        today = _today()
        with connect() as con:
            for t in requested:
                bare = t.split(".")[0]
                if t in found or bare in found:
                    # One good fetch wipes the slate: a transient outage must
                    # not accumulate into a permanent skip.
                    con.execute("DELETE FROM dead_tickers WHERE market=? AND ticker=?",
                                (market, bare))
                else:
                    con.execute(
                        "INSERT INTO dead_tickers (market,ticker,fails,first_failed,last_failed) "
                        "VALUES (?,?,1,?,?) ON CONFLICT(market,ticker) DO UPDATE SET "
                        "fails=fails+1, last_failed=excluded.last_failed",
                        (market, bare, today, today))
    except Exception as exc:                                   # noqa: BLE001
        print(f"  WARNING: could not record feed failures ({exc})")


def report(market: str) -> list[dict]:
    try:
        with connect() as con:
            return [dict(r) for r in con.execute(
                "SELECT ticker, fails, first_failed, last_failed FROM dead_tickers "
                "WHERE market=? ORDER BY fails DESC, ticker", (market,))]
    except Exception:                                          # noqa: BLE001
        return []
