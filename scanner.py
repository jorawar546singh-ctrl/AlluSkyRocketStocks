"""
Scanner v2 — orchestrator.
Pipeline per market:  universe -> bulk history -> trend filter (gate)
                      -> Darvas trigger -> relative-strength rank -> SQLite.

Usage:
    python scanner.py US
    python scanner.py IN
    python scanner.py US --intraday

Intraday mode (--intraday)
--------------------------
Runs the same pipeline while the market is still OPEN, so a forming breakout
shows up in the midday digest instead of waiting for the close. Signals it
saves are PROVISIONAL: the day's candle isn't finished, so the "close" above
the box and the volume ratio can both still change before 4pm.

To keep provisional data out of the measured edge, intraday rows are tagged
source='scanner_intraday', and the next EOD (non-intraday) run for that market
DELETES the same day's intraday rows before scanning. So each intraday signal
ends the day one of two ways:
    confirmed  -> rewritten by the EOD scan with the real closing price
    faded      -> deleted, because it never confirmed (it was a false alarm)

That purge matters mechanically, not just philosophically: the 21-day cooldown
would otherwise treat this morning's provisional row as "already seen" and skip
the confirmed one, and upsert_signal's ON CONFLICT DO NOTHING would drop it
again — so without the purge the permanent record would be the unconfirmed
price, and every outcome measured off it.
"""
import sys
from datetime import datetime, timezone

from core import universe as uni
from core.config import MARKETS
from core.datafeed import fetch_history
from core.db import connect, upsert_signal
from core.signals import darvas, relative_strength, trend_filter


def run(market_key: str, intraday: bool = False) -> list[dict]:
    cfg = MARKETS[market_key]
    mode = " [INTRADAY — provisional]" if intraday else ""
    print(f"=== AlluSkyRocketStocks scan: {cfg.label}{mode} ===")

    if not intraday:
        # This is the confirming EOD run. Clear today's provisional rows first
        # so (a) the cooldown doesn't treat them as already-seen and skip the
        # confirmed signal, and (b) intraday breakouts that faded before the
        # close don't linger as permanent false signals.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with connect() as con:
            ids = [r["id"] for r in con.execute(
                "SELECT id FROM signals WHERE market=? AND scan_date=? "
                "AND source='scanner_intraday'", (cfg.key, today))]
            if ids:
                ph = ",".join("?" * len(ids))
                con.execute(f"DELETE FROM outcomes WHERE signal_id IN ({ph})", ids)
                con.execute(f"DELETE FROM signals WHERE id IN ({ph})", ids)
                print(f"  cleared {len(ids)} provisional intraday row(s) from today")

    bare = (uni.us_universe(cfg) if cfg.key == "US"
            else uni.ca_universe() if cfg.key == "CA"
            else uni.in_universe())
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
                "source": "scanner_intraday" if intraday else "scanner",
            }
            upsert_signal(con, row)
            rows.append(row)

    rows.sort(key=lambda x: (x["rs_pct"] or 0), reverse=True)
    for w in uni.WARNINGS:
        print(f"  WARNING: {w}")
    label = "provisional signals (unconfirmed close)" if intraday else "signals"
    print(f"  saved {len(rows)} {label}\n")
    return rows[:cfg.top_n]


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    run(args[0] if args else "US", intraday="--intraday" in sys.argv)
