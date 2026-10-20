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
import json
import sys
from datetime import datetime, timezone

from core import universe as uni
from core.config import MARKETS
from core.datafeed import fetch_history
from core import company as comp
from core.db import connect, record_run, upsert_signal
from core.signals import darvas, regime, relative_strength, trend_filter


def run(market_key: str, intraday: bool = False) -> list[dict]:
    """Scan one market, recording the attempt whether or not it succeeds.

    The recording is the point. A market with no breakouts writes no signal
    rows, which used to be indistinguishable from a job that never ran --
    so a healthy quiet market and a dead workflow looked identical from the
    outside. Now every invocation leaves a row in `runs`.

    Errors are recorded and then RE-RAISED: the workflow must still fail
    loudly. Swallowing them here would trade one silent failure for another.
    """
    cfg = MARKETS[market_key]
    started = datetime.now(timezone.utc)
    stats = {"universe": None, "fetched": None, "eligible": None,
             "breakouts": None, "saved": None, "regime": None}

    def _record(status: str, error: str | None) -> None:
        record_run({
            "market": cfg.key,
            "run_ts": started.isoformat(timespec="seconds"),
            "run_date": started.strftime("%Y-%m-%d"),
            "source": "scanner_intraday" if intraday else "scanner",
            # run_type says WHAT the bars were, not when the job fired:
            # "close" means final daily bars, "intraday" means the session
            # was still open and the last bar can still change.
            "run_type": "intraday" if intraday else "close",
            "status": status,
            "duration_s": round(
                (datetime.now(timezone.utc) - started).total_seconds(), 1),
            "error": error,
            **stats,
        })

    try:
        rows = _scan(cfg, intraday, stats)
    except Exception as exc:                                   # noqa: BLE001
        _record("error", f"{type(exc).__name__}: {exc}"[:400])
        raise

    # Warnings are persisted even on success. They used to go only to the
    # Telegram digest, so the db -- and therefore the dashboard -- recorded
    # status=ok with no hint anything was wrong.
    notes = list(uni.WARNINGS)
    status = "ok" if stats["universe"] else "empty_universe"

    # A COLLAPSED universe is the dangerous case, and it was invisible: the
    # check above only caught a universe of exactly zero. On 2026-10-19 the
    # Finviz screener stopped returning rows, leaving only the Yahoo
    # most-active/gainers supplement -- 48 tickers instead of 380. Three runs
    # recorded status=ok and found no breakouts, which reads identically to a
    # genuinely quiet market. Scanning 12% of the universe and reporting
    # "nothing today" is worse than not scanning at all.
    #
    # Compared against this market's own recent history rather than a
    # hardcoded floor, so it keeps working as universe sizes change.
    typical = _typical_universe(cfg.key)
    if typical and stats["universe"] and stats["universe"] < typical * 0.5:
        status = "degraded"
        notes.append(f"universe collapsed to {stats['universe']} "
                     f"(recent median {typical}) — source likely broken; "
                     f"a 'no breakouts' result from this run means nothing")
    _record(status, " | ".join(notes)[:400] or None)
    return rows


def _save_universe(market: str, tickers: list[str]) -> None:
    """Remember a healthy ticker list so a broken source has a fallback."""
    try:
        with connect() as con:
            con.execute(
                "INSERT INTO universe_cache (market, saved_at, n, tickers) "
                "VALUES (?,?,?,?) ON CONFLICT(market) DO UPDATE SET "
                "saved_at=excluded.saved_at, n=excluded.n, tickers=excluded.tickers",
                (market, datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                 len(tickers), json.dumps(list(tickers))))
    except Exception as exc:                                   # noqa: BLE001
        print(f"  WARNING: could not cache universe ({exc})")


def _load_universe(market: str) -> list[str] | None:
    """The cached list, or -- if nothing is cached yet -- every ticker this
    market has ever signalled.

    The bootstrap matters because the cache is empty the first time the source
    breaks, which is exactly when it is needed. Those tickers are known to
    trade in the configured price range, so they are a usable stopgap.

    It is NOT a substitute for the real list: it only contains names that have
    already broken out once, so a scan restricted to it cannot discover
    anything new. It keeps the scanner working while the source is fixed, and
    the warning says so.
    """
    try:
        with connect() as con:
            r = con.execute("SELECT tickers FROM universe_cache WHERE market=?",
                            (market,)).fetchone()
            if r:
                return json.loads(r["tickers"])
            return [x[0] for x in con.execute(
                "SELECT DISTINCT ticker FROM signals WHERE market=? ORDER BY ticker",
                (market,))] or None
    except Exception:                                          # noqa: BLE001
        return None


def cached_age(market: str) -> str:
    try:
        with connect() as con:
            r = con.execute("SELECT saved_at FROM universe_cache WHERE market=?",
                            (market,)).fetchone()
        return r["saved_at"] if r else "bootstrap"
    except Exception:                                          # noqa: BLE001
        return "an earlier run"


def _typical_universe(market: str, lookback: int = 10) -> int | None:
    """Median universe size over this market's recent successful runs."""
    try:
        with connect() as con:
            # Healthy runs only. Including degraded ones would let a sustained
            # outage drag the median down until the collapsed universe became
            # the new "normal" and the alarm quietly stopped firing -- the
            # failure mode this check exists to prevent, rebuilt one level up.
            vals = [r[0] for r in con.execute(
                "SELECT universe FROM runs WHERE market=? AND universe IS NOT NULL "
                "AND status='ok' ORDER BY run_ts DESC LIMIT ?",
                (market, lookback))]
        if len(vals) < 3:
            return None                 # not enough history to judge
        vals.sort()
        return vals[len(vals) // 2]
    except Exception:                                          # noqa: BLE001
        return None


def _record_boxes(cfg, histories: dict, eligible: dict) -> None:
    """Record the current Darvas box for every eligible ticker.

    One row per (ticker, formed_date); re-running a day is idempotent. A box
    is marked broken the first time the close leaves it, which is what a
    trailing stop follows.

    Best-effort: a failure here costs box history, never a scan.
    """
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with connect() as con:
            for t in eligible:
                df = histories.get(t)
                if df is None or len(df) < cfg.darvas_box_days + 1:
                    continue
                bare = t.removesuffix(cfg.ticker_suffix)
                box = df.iloc[-(cfg.darvas_box_days + 1):-1]
                top, bottom = float(box["High"].max()), float(box["Low"].min())
                close = float(df["Close"].iloc[-1])

                prev = con.execute(
                    "SELECT id, box_top, box_bottom FROM boxes WHERE market=? AND ticker=? "
                    "AND broken_date IS NULL ORDER BY formed_date DESC LIMIT 1",
                    (cfg.key, bare)).fetchone()
                if prev and close > prev["box_top"]:
                    con.execute("UPDATE boxes SET broken_date=? WHERE id=?",
                                (today, prev["id"]))
                    prev = None
                if prev is None:
                    con.execute(
                        "INSERT INTO boxes (market,ticker,box_top,box_bottom,formed_date) "
                        "VALUES (?,?,?,?,?) ON CONFLICT(market,ticker,formed_date) DO NOTHING",
                        (cfg.key, bare, round(top, 4), round(bottom, 4), today))
    except Exception as exc:                                # noqa: BLE001
        print(f"  WARNING: box history not recorded ({exc})")


def _scan(cfg, intraday: bool, stats: dict) -> list[dict]:
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

    # If the ticker source broke, scan the last known-good list rather than
    # the scraps. On 2026-10-19 the Finviz screener stopped returning rows and
    # the US universe fell from 380 names to 48 -- three runs then reported
    # "no breakouts", which is true of 48 tickers and says nothing about the
    # market. A list a few days old misses new listings; scanning 12% of the
    # market misses almost everything.
    typical = _typical_universe(cfg.key)
    if typical and len(bare) < typical * 0.5:
        cached = _load_universe(cfg.key)
        if cached and len(cached) > len(bare):
            src = cached_age(cfg.key)
            uni.WARNINGS.append(
                f"ticker source returned only {len(bare)} names (usually ~{typical}); "
                f"scanning {len(cached)} from " +
                (f"the cached list saved {src}" if src != "bootstrap"
                 else "previously-signalled tickers (bootstrap — cannot find NEW names)") +
                " — fix the source to restore full coverage")
            bare = cached
    elif len(bare) >= 50:
        _save_universe(cfg.key, bare)

    # Tips from Discord / the manual watchlist join the universe -- they do not
    # bypass anything downstream, they just get looked at. A screener that only
    # ever sees what the screener found cannot act on an idea from outside it.
    mentioned = uni.mentioned_recently(cfg.key)
    extra = [t for t in mentioned if t not in set(bare)]
    if extra:
        bare = list(bare) + extra
        print(f"  universe: +{len(extra)} from mentions ({', '.join(extra[:8])}"
              f"{'…' if len(extra) > 8 else ''})")
    stats["mentions_added"] = len(extra)
    stats["universe"] = len(bare)
    if not bare:
        return []
    tickers = [b + cfg.ticker_suffix for b in bare]

    histories = fetch_history(tickers + [cfg.benchmark], period="1y")
    bench_df = histories.pop(cfg.benchmark, None)
    if bench_df is None:
        uni.WARNINGS.append(f"benchmark {cfg.benchmark} fetch failed — rs_excess will be null")

    # 0) Market regime — INFORMATION ONLY, gates nothing. Read off the same
    #    benchmark already fetched above for relative strength.
    mkt_regime = regime.evaluate(bench_df)
    stats["fetched"] = len(histories)
    stats["regime"] = mkt_regime.get("label")
    print(f"  market regime: {mkt_regime['label']} ({mkt_regime['detail']})")

    # 1) Trend gate — kills ineligible stocks before the trigger ever runs
    eligible: dict[str, dict] = {}
    for t, df in histories.items():
        tf = trend_filter.evaluate(df, cfg.trend_near_high_pct)
        if tf["pass"]:
            eligible[t] = tf
    stats["eligible"] = len(eligible)
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
    stats["breakouts"] = len(hits)
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
                # Read off the universe page the scan already fetched. None
                # when that page carried no sector for this name.
                "sector": uni.SECTORS.get(bare_t),
                "company_name": uni.NAMES.get(bare_t),
                "industry": uni.INDUSTRIES.get(bare_t),
                "source": "scanner_intraday" if intraday else "scanner",
            }
            upsert_signal(con, row)
            rows.append(row)

    # Company metadata for the handful of names that actually fired, not for
    # the 400-ticker universe: one request each, only when a signal is new.
    for row in rows:
        info = comp.lookup(row["ticker"], cfg.key)
        for k in ("company_name", "sector", "industry", "next_earnings_date"):
            if info.get(k) and not row.get(k):
                row[k] = info[k]
    if rows:
        with connect() as con:
            for row in rows:
                con.execute(
                    "UPDATE signals SET company_name=COALESCE(?,company_name),"
                    " sector=COALESCE(?,sector), industry=COALESCE(?,industry),"
                    " next_earnings_date=COALESCE(?,next_earnings_date)"
                    " WHERE market=? AND ticker=? AND scan_date=?",
                    (row.get("company_name"), row.get("sector"),
                     row.get("industry"), row.get("next_earnings_date"),
                     cfg.key, row["ticker"], row["scan_date"]))

    # Box history: every box each eligible ticker is currently sitting in, so
    # a trailing stop can follow the newest box bottom rather than the one
    # that happened to exist at signal time.
    _record_boxes(cfg, histories, eligible)

    stats["saved"] = len(rows)
    rows.sort(key=lambda x: (x["rs_pct"] or 0), reverse=True)
    for w in uni.WARNINGS:
        print(f"  WARNING: {w}")
    label = "provisional signals (unconfirmed close)" if intraday else "signals"
    print(f"  saved {len(rows)} {label}\n")
    return rows[:cfg.top_n]


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    run(args[0] if args else "US", intraday="--intraday" in sys.argv)
