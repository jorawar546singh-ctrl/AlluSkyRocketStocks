#!/usr/bin/env python3
"""
Clear every stored close so the next export re-decides them.

Needed because closes are deliberately sticky: _apply_lifecycle skips any
signal that already carries a close_reason, so a corrected walk would never
revisit the verdicts the buggy one wrote. Nothing here is destructive in the
sense that matters -- close fields are all derived from price history and are
recomputed from scratch on the next export.

    python3 reset_closes.py --dry-run
    python3 reset_closes.py
    python3 reset_closes.py --market US

Also drops the stored stop studies, which were computed through the same walk
and are wrong for the same reason.
"""
import argparse

from core.config import MARKETS
from core.db import connect

FIELDS = ["close_date", "close_reason", "exit_price", "final_return_pct",
          "hit10_before_stop", "hit10_date", "max_gain_pct", "max_dd_pct",
          "peak_gain_pct", "trough_dd_pct"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default=None, help="limit to one market")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.market and a.market not in MARKETS:
        print(f"unknown market {a.market}; known: {list(MARKETS)}")
        return 2

    where, params = "close_reason IS NOT NULL", []
    if a.market:
        where += " AND market=?"
        params.append(a.market)

    with connect() as con:
        rows = con.execute(
            f"SELECT market, close_reason, COUNT(*) n FROM signals WHERE {where} "
            "GROUP BY market, close_reason ORDER BY market, n DESC", params).fetchall()
        total = sum(r["n"] for r in rows)
        for r in rows:
            print(f"  {r['market']}  {r['close_reason']:<16} {r['n']}")
        print(f"  -> {total} closed signal(s) would be reopened for re-evaluation")

        if a.dry_run:
            print("  (dry run — nothing changed)")
            return 0
        if not total:
            print("  nothing to reset")
            return 0

        con.execute(f"UPDATE signals SET {', '.join(f'{f}=NULL' for f in FIELDS)} "
                    f"WHERE {where}", params)
        # Studies were computed through the same walk, so they carry the same
        # error. Dropping them is better than leaving numbers that look valid.
        if a.market:
            con.execute("DELETE FROM studies WHERE kind='stops' AND market=?", (a.market,))
        else:
            con.execute("DELETE FROM studies WHERE kind='stops'")
        print(f"  reset {total} signal(s); stored stop studies dropped")
        print("  run export_dashboard.py to re-decide closes, then run_stop_study.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
