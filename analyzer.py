"""
Performance analyzer — the "is the edge real?" engine.

Backfills forward returns for every signal old enough to measure, then prints
(and stores in the db) the only stats that matter:

  hit rate @7/14/30d, avg & median return, expectancy per signal,
  stop-hit rate, factor splits (RS quartile, vol_ratio, legacy grade).

Run after the scanner in the same workflow:
    python analyzer.py US
    python analyzer.py IN
"""
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

from core.config import MARKETS
from core.datafeed import fetch_history
from core.db import connect

MIN_AGE_DAYS = 7   # a signal must be at least this old to score anything


def backfill(market_key: str):
    cfg = MARKETS[market_key]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MIN_AGE_DAYS)).strftime("%Y-%m-%d")

    with connect() as con:
        rows = con.execute(
            "SELECT s.* FROM signals s LEFT JOIN outcomes o ON o.signal_id = s.id "
            "WHERE s.market=? AND s.scan_date <= ? "
            "AND (o.signal_id IS NULL OR (o.ret_d30 IS NULL AND "
            "     julianday('now') - julianday(s.scan_date) <= 60))",
            (market_key, cutoff),
        ).fetchall()
    if not rows:
        print(f"{market_key}: no signals need outcome backfill")
        return

    tickers = sorted({r["ticker"] + cfg.ticker_suffix for r in rows})
    print(f"{market_key}: backfilling outcomes for {len(rows)} signals / {len(tickers)} tickers")
    histories = fetch_history(tickers, period="1y")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as con:
        for r in rows:
            df = histories.get(r["ticker"] + cfg.ticker_suffix)
            if df is None or r["price"] <= 0:
                continue
            df = df.copy()
            df.index = pd.to_datetime(df.index).tz_localize(None)
            window = df[df.index > pd.Timestamp(r["scan_date"])]
            if window.empty:
                continue
            entry = r["price"]

            def ret_at(days):
                w = window[window.index <= pd.Timestamp(r["scan_date"]) + pd.Timedelta(days=days)]
                if w.empty:
                    return None
                # require the window to actually span the horizon
                if (w.index[-1] - pd.Timestamp(r["scan_date"])).days < days - 3:
                    return None
                return round((float(w["Close"].iloc[-1]) - entry) / entry * 100, 2)

            w30 = window[window.index <= pd.Timestamp(r["scan_date"]) + pd.Timedelta(days=30)]
            max_gain = round((float(w30["High"].max()) - entry) / entry * 100, 2) if len(w30) else None
            max_dd = round((float(w30["Low"].min()) - entry) / entry * 100, 2) if len(w30) else None
            stop_hit = (int(float(w30["Low"].min()) <= r["suggested_stop"])
                        if len(w30) and r["suggested_stop"] else None)

            con.execute(
                "INSERT INTO outcomes (signal_id,ret_d7,ret_d14,ret_d30,max_gain_d30,"
                "max_dd_d30,stop_hit_d30,computed_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(signal_id) DO UPDATE SET ret_d7=excluded.ret_d7,"
                "ret_d14=excluded.ret_d14, ret_d30=excluded.ret_d30,"
                "max_gain_d30=excluded.max_gain_d30, max_dd_d30=excluded.max_dd_d30,"
                "stop_hit_d30=excluded.stop_hit_d30, computed_at=excluded.computed_at",
                (r["id"], ret_at(7), ret_at(14), ret_at(30), max_gain, max_dd, stop_hit, now),
            )


def report(market_key: str) -> dict:
    with connect() as con:
        df = pd.read_sql_query(
            "SELECT s.*, o.ret_d7, o.ret_d14, o.ret_d30, o.max_gain_d30, o.max_dd_d30, "
            "o.stop_hit_d30 FROM signals s JOIN outcomes o ON o.signal_id = s.id "
            "WHERE s.market = ?", con, params=(market_key,))
    if df.empty:
        print(f"{market_key}: no measured outcomes yet")
        return {}

    def stats(col):
        d = df[col].dropna()
        if d.empty:
            return None
        return {"n": int(len(d)), "hit_rate": round((d > 0).mean() * 100, 1),
                "avg": round(d.mean(), 2), "median": round(d.median(), 2)}

    out = {
        "n_signals": int(len(df)),
        "d7": stats("ret_d7"), "d14": stats("ret_d14"), "d30": stats("ret_d30"),
        "expectancy_d30": stats("ret_d30")["avg"] if stats("ret_d30") else None,
        "stop_hit_rate": round(df["stop_hit_d30"].dropna().mean() * 100, 1)
                         if df["stop_hit_d30"].notna().any() else None,
        "avg_max_gain_d30": round(df["max_gain_d30"].dropna().mean(), 2)
                            if df["max_gain_d30"].notna().any() else None,
        "by_grade": {}, "by_rs_quartile": {},
    }
    if df["legacy_grade"].notna().any():
        for g, gdf in df.dropna(subset=["legacy_grade", "ret_d30"]).groupby("legacy_grade"):
            if len(gdf) >= 3:
                out["by_grade"][g] = {"n": int(len(gdf)),
                                      "avg_d30": round(gdf["ret_d30"].mean(), 2),
                                      "hit_rate": round((gdf["ret_d30"] > 0).mean() * 100, 1)}
    if df["rs_pct"].notna().any():
        q = pd.qcut(df["rs_pct"].dropna(), 4, labels=["Q1", "Q2", "Q3", "Q4"], duplicates="drop")
        for label, gdf in df.dropna(subset=["rs_pct", "ret_d30"]).groupby(q, observed=True):
            out["by_rs_quartile"][str(label)] = {"n": int(len(gdf)),
                                                 "avg_d30": round(gdf["ret_d30"].mean(), 2)}
    print(f"{market_key} edge report: {out}")
    return out


if __name__ == "__main__":
    mk = sys.argv[1] if len(sys.argv) > 1 else "US"
    backfill(mk)
    report(mk)
