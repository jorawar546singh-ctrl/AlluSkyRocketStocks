"""
One-time cleanup: remove duplicate signals created by migrating v1's
daily watchlist CSVs (same ticker, same box, consecutive days).

Rule: a later signal is a duplicate if an earlier signal exists for the same
market+ticker within 21 calendar days (a new 14-session box cannot form faster).
The EARLIEST record of each breakout is kept; orphaned outcomes are removed.

Run from repo root:  python3 dedupe.py
Then:                python3 export_dashboard.py  &&  commit db + data.json
"""
from core.db import connect

DUP_SQL = """
SELECT s2.id FROM signals s1
JOIN signals s2
  ON s1.market = s2.market AND s1.ticker = s2.ticker
 AND s2.scan_date > s1.scan_date
 AND julianday(s2.scan_date) - julianday(s1.scan_date) <= 21
"""

with connect() as con:
    before = {r["market"]: r["n"] for r in
              con.execute("SELECT market, COUNT(*) n FROM signals GROUP BY market")}
    dup_ids = [r["id"] for r in con.execute(DUP_SQL)]
    if dup_ids:
        ph = ",".join("?" * len(dup_ids))
        con.execute(f"DELETE FROM outcomes WHERE signal_id IN ({ph})", dup_ids)
        con.execute(f"DELETE FROM signals  WHERE id IN ({ph})", dup_ids)
    con.execute("DELETE FROM outcomes WHERE signal_id NOT IN (SELECT id FROM signals)")
    after = {r["market"]: r["n"] for r in
             con.execute("SELECT market, COUNT(*) n FROM signals GROUP BY market")}

for m in sorted(before):
    print(f"{m}: {before[m]} -> {after.get(m, 0)} signals "
          f"({before[m] - after.get(m, 0)} duplicates removed)")
print("Done. Now run: python3 export_dashboard.py, then commit data/asr.db + data.json")
