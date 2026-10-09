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
        # sd travels with the average so the dashboard can tell a real edge
        # from one that is just noise with a positive sign. A 30-day mean of
        # +0.5% across 160 signals with a 12% spread is inside one standard
        # error of zero -- presenting that as "edge" is how you talk yourself
        # into trading a coin flip.
        return {"n": int(len(d)), "hit_rate": round((d > 0).mean() * 100, 1),
                "avg": round(d.mean(), 2), "median": round(d.median(), 2),
                "sd": round(float(d.std()), 2) if len(d) > 1 else None}

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
    if df["rs_pct"].notna().sum() >= 8:
        rs = df.dropna(subset=["rs_pct", "ret_d30"])
        try:
            q = pd.qcut(rs["rs_pct"], 4, labels=False, duplicates="drop")
            for label, gdf in rs.groupby(q, observed=True):
                out["by_rs_quartile"][f"Q{int(label) + 1}"] = {
                    "n": int(len(gdf)), "avg_d30": round(gdf["ret_d30"].mean(), 2)}
        except (ValueError, IndexError):
            pass   # not enough spread to bucket yet — leave empty
    print(f"{market_key} edge report: {out}")
    return out


def backfill_mentions(market_key: str) -> None:
    """Forward returns for tickers somebody else flagged, measured from the tip.

    Scored from price_at_mention, NOT from any later breakout price, because
    the question is "was following this channel worth it", and a follower buys
    when the tip lands. Anchoring to a breakout would quietly measure the
    Darvas rules again and flatter the channel.
    """
    cfg = MARKETS[market_key]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MIN_AGE_DAYS)).strftime("%Y-%m-%d")
    with connect() as con:
        rows = con.execute(
            "SELECT m.* FROM mentions m LEFT JOIN mention_outcomes o "
            "ON o.mention_id = m.id WHERE m.market=? AND m.mention_date <= ? "
            "AND (o.mention_id IS NULL OR (o.ret_d30 IS NULL AND "
            "     julianday('now') - julianday(m.mention_date) <= 60))",
            (market_key, cutoff)).fetchall()
    if not rows:
        print(f"{market_key}: no mentions need outcome backfill")
        return

    tickers = sorted({r["ticker"] + cfg.ticker_suffix for r in rows})
    print(f"{market_key}: backfilling outcomes for {len(rows)} mention(s) / "
          f"{len(tickers)} ticker(s)")
    histories = fetch_history(tickers, period="1y")

    done = 0
    with connect() as con:
        for r in rows:
            df = histories.get(r["ticker"] + cfg.ticker_suffix)
            if df is None or df.empty:
                continue
            base = r["price_at_mention"]
            if not base:
                continue
            after = df[df.index >= pd.Timestamp(r["mention_date"], tz=df.index.tz)]
            if after.empty:
                continue

            def ret(days):
                w = after.head(days)
                if len(w) < min(days, 3):      # too thin to call
                    return None
                return round((w["Close"].iloc[-1] / base - 1) * 100, 2)

            w30 = after.head(30)
            con.execute(
                "INSERT INTO mention_outcomes (mention_id,ret_d7,ret_d14,ret_d30,"
                "max_gain_d30,max_dd_d30,computed_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(mention_id) DO UPDATE SET ret_d7=excluded.ret_d7,"
                "ret_d14=excluded.ret_d14,ret_d30=excluded.ret_d30,"
                "max_gain_d30=excluded.max_gain_d30,max_dd_d30=excluded.max_dd_d30,"
                "computed_at=excluded.computed_at",
                (r["id"], ret(7), ret(14), ret(30),
                 round((w30["High"].max() / base - 1) * 100, 2) if len(w30) else None,
                 round((w30["Low"].min() / base - 1) * 100, 2) if len(w30) else None,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")))
            done += 1
    print(f"{market_key}: scored {done} mention(s)")


def mention_report(market_key: str) -> dict:
    """Is the tip source actually any good?

    Reported per source and never merged into the signal edge report. The two
    answer different questions -- "do my rules work" vs "is this channel worth
    following" -- and averaging them together would destroy both.
    """
    with connect() as con:
        df = pd.read_sql_query(
            "SELECT m.source, m.ticker, m.mention_date, o.ret_d7, o.ret_d14, "
            "o.ret_d30, o.max_gain_d30, o.max_dd_d30 FROM mentions m "
            "JOIN mention_outcomes o ON o.mention_id = m.id WHERE m.market=?",
            con, params=(market_key,))
        total = con.execute("SELECT COUNT(*) FROM mentions WHERE market=?",
                            (market_key,)).fetchone()[0]
        # Same window the scanner uses to pull tips into the universe, so the
        # TIP badge marks exactly the tickers a tip put there.
        recent = [r[0] for r in con.execute(
            "SELECT DISTINCT ticker FROM mentions WHERE market=? "
            "AND julianday('now') - julianday(mention_date) <= 14",
            (market_key,))]
    out = {"n_mentions": int(total), "recent": recent, "by_source": {}}
    if df.empty:
        return out

    for source, sdf in df.groupby("source"):
        scored = sdf.dropna(subset=["ret_d30"])
        row = {"n": int(len(sdf)), "measured": int(len(scored))}
        if len(scored):
            row["avg_d30"] = round(float(scored["ret_d30"].mean()), 2)
            row["hit_d30"] = round(float((scored["ret_d30"] > 0).mean()) * 100, 1)
            row["sd"] = round(float(scored["ret_d30"].std()), 2) if len(scored) > 1 else None
            row["avg_max_gain"] = round(float(scored["max_gain_d30"].mean()), 2)
            row["avg_max_dd"] = round(float(scored["max_dd_d30"].mean()), 2)
        out["by_source"][source] = row
    return out


def cohorts(market_key: str) -> dict:
    """Month-by-month measured outcomes, plus what is still maturing.

    Read straight from the db on purpose. The dashboard payload drops any
    signal older than 30 days unless it is still TRENDING, so anything
    computed from data.json for an old month is survivors only -- that is how
    a month whose real 30-day outcome was -10.2% can read "+18.9%, 100% up".
    The db keeps every signal that ever fired, which is the only honest basis
    for "how did June actually do".

    'pending' counts signals too young to have a 30-day number yet but still
    inside the backfill window (see MIN_AGE_DAYS / the 60-day bound in
    backfill()). Those are the open question, not a gap in the data.
    """
    with connect() as con:
        df = pd.read_sql_query(
            "SELECT s.scan_date, s.trend_pass, o.ret_d30, o.stop_hit_d30, "
            "julianday('now') - julianday(s.scan_date) AS age "
            "FROM signals s LEFT JOIN outcomes o ON o.signal_id = s.id "
            "WHERE s.market = ?", con, params=(market_key,))
    if df.empty:
        return {}

    df["month"] = df["scan_date"].str.slice(0, 7)
    out = {"by_month": [], "pending": None}

    for month, mdf in df.groupby("month", sort=True):
        scored = mdf.dropna(subset=["ret_d30"])
        row = {"month": month, "n": int(len(mdf)), "measured": int(len(scored))}
        if len(scored):
            row["avg_d30"] = round(float(scored["ret_d30"].mean()), 2)
            row["hit_d30"] = round(float((scored["ret_d30"] > 0).mean()) * 100, 1)
            if scored["stop_hit_d30"].notna().any():
                row["stop_hit"] = round(
                    float(scored["stop_hit_d30"].dropna().mean()) * 100, 1)
        # The trend gate landed mid-history; a month that straddles it is not
        # comparable to one either side, so say so rather than quietly mixing.
        gated = mdf["trend_pass"].notna()
        row["gated"] = "all" if gated.all() else "none" if not gated.any() else "mixed"
        out["by_month"].append(row)

    # Two different things look identical in the db (ret_d30 IS NULL) and must
    # not be conflated:
    #   maturing  - younger than 30 days, so no 30-day number CAN exist yet.
    #               This is the open question, and it has a date attached.
    #   unmeasured- old enough to score but still blank, meaning backfill could
    #               not price it (delisted, ticker change, fetch failure). This
    #               is missing data, and silently counting it as "pending"
    #               would mean waiting forever for a verdict that never lands.
    blank = df[df["ret_d30"].isna()]
    maturing = blank[blank["age"] < 30]
    unmeasured = blank[(blank["age"] >= 30) & (blank["age"] <= 60)]
    if len(maturing) or len(unmeasured):
        out["pending"] = {"n": int(len(maturing)),
                          "unmeasured": int(len(unmeasured))}
        if len(maturing):
            newest = maturing["scan_date"].max()
            out["pending"]["newest"] = newest
            # A signal is readable 30 days after it fired, so the whole batch
            # is readable 30 days after the newest one in it.
            out["pending"]["all_mature"] = (
                datetime.fromisoformat(newest) + timedelta(days=30)
            ).strftime("%Y-%m-%d")
    return out


if __name__ == "__main__":
    mk = sys.argv[1] if len(sys.argv) > 1 else "US"
    backfill(mk)
    report(mk)
    # Tips are scored on their own clock and reported separately — see
    # mention_report's docstring for why these never merge.
    backfill_mentions(mk)
    mr = mention_report(mk)
    if mr["n_mentions"]:
        print(f"{mk} mention report: {mr}")
    print(f"{mk} cohorts: {cohorts(mk)}")
