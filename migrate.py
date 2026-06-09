"""
One-time migration: legacy v1 artifacts -> data/asr.db

Sources (any that exist are picked up):
  <repo>/breakouts_*.csv          per-scan snapshots (richest rows)
  <repo>/alerted_history.json     first-alert record (fills gaps, sets earliest date)
  <repo>/positions.json           open positions

Usage:
    python migrate.py US /path/to/AlluSkyRocketStocks
    python migrate.py IN /path/to/clueless-indian-bot
"""
import glob
import json
import os
import re
import sys

import pandas as pd

from core.db import connect, upsert_signal


def _num(v):
    try:
        f = float(v)
        return f if f == f else None   # NaN guard
    except (TypeError, ValueError):
        return None


def migrate(market: str, repo: str):
    inserted_csv = inserted_hist = inserted_pos = 0

    with connect() as con:
        # ---- breakouts_*.csv ------------------------------------------------
        for path in sorted(glob.glob(os.path.join(repo, "breakouts_*.csv"))):
            m = re.search(r"breakouts_(\d{8})_(\d{4})", os.path.basename(path))
            if not m:
                continue
            d, t = m.group(1), m.group(2)
            scan_date = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            scan_ts = f"{scan_date}T{t[:2]}:{t[2:]}:00"
            try:
                df = pd.read_csv(path)
            except Exception as e:                          # noqa: BLE001
                print(f"  skip {path}: {e}")
                continue
            for _, r in df.iterrows():
                if not isinstance(r.get("ticker"), str):
                    continue
                upsert_signal(con, {
                    "market": market,
                    "ticker": r["ticker"].removesuffix(".NS"),
                    "scan_ts": scan_ts, "scan_date": scan_date,
                    "price": _num(r.get("price")) or 0,
                    "box_top": _num(r.get("box_top")),
                    "box_bottom": _num(r.get("box_bottom")),
                    "pct_above_box": _num(r.get("pct_above_box")),
                    "vol_ratio": _num(r.get("vol_ratio")),
                    "suggested_stop": _num(r.get("suggested_stop")),
                    "risk_pct": _num(r.get("risk_pct")),
                    "rr_ratio": _num(r.get("rr_ratio")),
                    "legacy_score": int(_num(r.get("score")) or 0) or None,
                    "legacy_grade": r.get("grade") if isinstance(r.get("grade"), str) else None,
                    "legacy_tier": r.get("tier") if isinstance(r.get("tier"), str) else None,
                    "source": "migration_csv",
                })
                inserted_csv += 1

        # ---- alerted_history.json -------------------------------------------
        hist_path = os.path.join(repo, "alerted_history.json")
        if os.path.exists(hist_path):
            for e in json.load(open(hist_path)):
                ts = e.get("first_alerted", "")
                if not ts:
                    continue
                upsert_signal(con, {
                    "market": market,
                    "ticker": e["ticker"].removesuffix(".NS"),
                    "scan_ts": ts, "scan_date": ts[:10],
                    "price": _num(e.get("breakout_price")) or 0,
                    "box_top": _num(e.get("breakout_box_top")),
                    "legacy_score": e.get("original_score"),
                    "legacy_grade": e.get("original_grade"),
                    "legacy_tier": e.get("original_tier"),
                    "source": "migration_history",
                })
                inserted_hist += 1

        # ---- positions.json --------------------------------------------------
        pos_path = os.path.join(repo, "positions.json")
        if os.path.exists(pos_path):
            for p in json.load(open(pos_path)).get("positions", []):
                con.execute(
                    "INSERT INTO positions (market,ticker,shares,entry_price,entry_date,"
                    "initial_stop,notes) VALUES (?,?,?,?,?,?,?)",
                    (market, p["ticker"].removesuffix(".NS"), p["shares"], p["entry_price"],
                     p["entry_date"], p["initial_stop"], p.get("notes", "")),
                )
                inserted_pos += 1

        n = con.execute("SELECT COUNT(*) c FROM signals WHERE market=?", (market,)).fetchone()["c"]

    print(f"{market}: processed {inserted_csv} csv rows + {inserted_hist} history rows "
          f"+ {inserted_pos} positions -> {n} unique signals in db")


if __name__ == "__main__":
    migrate(sys.argv[1], sys.argv[2])
