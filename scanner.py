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

    bare = uni.us_universe() if cfg.key == "US" else uni.in_universe()
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
    for t in eligible:
        d = darvas.evaluate(histories[t], cfg.darvas_box_days, cfg.volume_multiplier)
        if d and cfg.min_price <= d["price"] <= cfg.max_price:
            hits[t] = d
    print(f"  darvas trigger: {len(hits)} breakouts")

    # 3) RS percentile across everything scanned today (broad base = honest rank)
    rs = relative_strength.rank(histories, bench_df, cfg.rs_lookback_days)

    now = datetime.now(timezone.utc)
    rows = []
    with connect() as con:
        for t, d in hits.items():
            tf, r = eligible[t], rs.get(t, {})
            row = {
                "market": cfg.key,
                "ticker": t.removesuffix(cfg.ticker_suffix),
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
