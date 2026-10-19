#!/usr/bin/env python3
"""
Replay closed signals against alternative stop levels and store the result.

Separate from the export on purpose: it needs price history for CLOSED
signals, and the export stopped downloading those once signals began being
kept forever rather than deleted. Run it weekly -- it is a slow-moving
statistic, and re-running it daily would re-download several hundred tickers
to move a number by a tenth of a point.

    python3 run_stop_study.py            # every market
    python3 run_stop_study.py US         # one market

Results land in the `studies` table and are picked up by the next export.
"""
import json
import sys

import pandas as pd

from core import stop_study
from core.config import MARKETS
from core.datafeed import fetch_history
from core.db import connect

# Bars have to reach back to each signal's date for the walk to be valid, and
# the close lifecycle expires a signal at 180 days, so 2 years covers every
# signal that can still produce a usable answer.
PERIOD = "2y"


def main(keys: list[str]) -> int:
    for key in keys:
        cfg = MARKETS[key]
        with connect() as con:
            rows = con.execute(
                "SELECT id, ticker, scan_date, price, box_bottom, suggested_stop "
                "FROM signals WHERE market=? AND close_reason IS NOT NULL "
                "ORDER BY scan_date", (key,)).fetchall()
            signals = [dict(r) for r in rows]

        if not signals:
            print(f"{key}: no closed signals yet — nothing to study")
            continue

        tickers = sorted({s["ticker"] + cfg.ticker_suffix for s in signals})
        print(f"{key}: {len(signals)} closed signals, {len(tickers)} tickers")
        try:
            frames = fetch_history(tickers, period=PERIOD)
        except Exception as e:                              # noqa: BLE001
            print(f"  fetch failed ({e}) — skipping {key}")
            continue

        payload = stop_study.run(signals, frames, cfg.ticker_suffix)
        with connect() as con:
            stop_study.save(con, key, payload)

        print(f"  walkable: {payload['n_walkable']}/{payload['n_signals']}")
        print(f"  {'variant':<20}{'n':>6}{'+10% first':>12}{'stopped':>10}"
              f"{'avg ret':>10}{'median':>9}{'t':>7}")
        for v in payload["variants"]:
            pct = lambda x, s='%': '—' if x is None else f"{x}{s}"     # noqa: E731
            print(f"  {v['label']:<20}{v['n']:>6}{pct(v['hit10_rate']):>12}"
                  f"{pct(v['stop_rate']):>10}{pct(v['avg_return']):>10}"
                  f"{pct(v['median_return']):>9}{pct(v['t'],''):>7}")
        print("  (t below 2 = the average is inside the noise, whatever its sign)")
    return 0


if __name__ == "__main__":
    args = [a.upper() for a in sys.argv[1:]] or list(MARKETS)
    bad = [a for a in args if a not in MARKETS]
    if bad:
        print(f"unknown market(s): {bad}; known: {list(MARKETS)}")
        raise SystemExit(2)
    raise SystemExit(main(args))
