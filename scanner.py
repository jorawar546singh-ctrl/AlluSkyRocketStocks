"""
Scanner v2 — orchestrator.
Pipeline per market:  universe -> bulk history -> trend filter (gate)
                      -> Darvas trigger -> relative-strength rank -> SQLite.

Usage:
    python scanner.py US
    python scanner.py IN
"""
import sys
from datetime import datetime, timezone

from core import universe as uni
from core.config import MARKETS
from core.datafeed import fetch_history
from core.db import connect, upsert_signal
from core.signals import darvas, relative_strength, trend_filter


def run(market_key: str) -> list[dict]:
    cfg = MARKETS[market_key]
    print(f"=== AlluSkyRocketStocks scan: {cfg.label} ===")

    bare = uni.us_universe(cfg) if cfg.key == "US" else uni.in_universe()
    if not bare:
        return []
    tickers = [b + cfg.ticker_suffix for b in bare]

    histories = fetch_history(tickers + [cfg.benchmark], period="1y")
    bench_df = histories.pop(cfg.benchmark, None)
    if bench_df is None:
        uni.WARNINGS.append(f"benchmark {cfg.benchmark} fetch failed — rs_excess will be null")

    # 1) Trend gate — kills ineligible stocks before the trigger ever runs
    eligible: dict[str, dict] = {}
    for t, df in histories.items():
        tf = trend_filter.evaluate(df, cfg.trend_near_high_pct)
        if tf["pass"]:
            eligible[t] = tf
    print(f"  trend gate: {len(eligible)}/{len(histories)} eligible")

    # 2) Darvas trigger on eligible names only
    hits: dict[str, dict] = {}
    dropped_by_price = 0
    for t in eligible:
        d = darvas.evaluate(histories[t], cfg.darvas_box_days, cfg.volume_multiplier)
        if not d:
            continue
        if cfg.min_price <= d["price"] <= cfg.max_price:
            hits[t] = d
        else:
            dropped_by_price += 1
    print(f"  darvas trigger: {len(hits)} breakouts"
          f"{f'  ({dropped_by_price} dropped outside ${cfg.min_price}-${cfg.max_price})' if dropped_by_price else ''}")

    # 3) RS percentile across everything scanned today (broad base = honest rank)
    rs = relative_strength.rank(histories, bench_df, cfg.rs_lookback_days)

    now = datetime.now(timezone.utc)
    rows = []
    with connect() as con:
        for t, d in hits.items():
            bare_t = t.removesuffix(cfg.ticker_suffix)
            # Cooldown: a new 14-session box cannot form in under ~21
            # calendar days, so any repeat within 21 days is the same breakout.
            dup = con.execute(
                "SELECT 1 FROM signals WHERE market=? AND ticker=? "
                "AND julianday(?) - julianday(scan_date) <= 21",
                (cfg.key, bare_t, now.strftime("%Y-%m-%d"))).fetchone()
            if dup:
                continue
            tf, r = eligible[t], rs.get(t, {})
            row = {
                "market": cfg.key,
                "ticker": bare_t,
                "scan_ts": now.isoformat(timespec="seconds"),
                "scan_date": now.strftime("%Y-%m-%d"),
                **d,
                "rs_pct": r.get("rs_pct"),
                "rs_excess": r.get("rs_excess"),
                "trend_pass": 1,
                "trend_detail": tf["detail"],
                "dist_52w_high": tf["dist_52w_high"],
                "source": "scanner",
            }
            upsert_signal(con, row)
            rows.append(row)

    rows.sort(key=lambda x: (x["rs_pct"] or 0), reverse=True)
    for w in uni.WARNINGS:
        print(f"  WARNING: {w}")
    print(f"  saved {len(rows)} signals\n")
    return rows[:cfg.top_n]


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "US")
