"""
Export data.json — the single payload the static dashboard reads.

v2.1: each recent signal is enriched with live tracking data so the
dashboard can show the v1 watchlist columns:

  now_price, gain_pct      current close, % since breakout print
  age_days                 calendar days since signal
  streak / peak_streak     consecutive day-over-day HIGHER closes (current / best),
                           v1 logic — resets on any non-higher close
  vol_today / box_today    today's checks: volume >= 2x 20d avg, close > box top
  status                   TRENDING | WATCHING | FADING (mechanical, defined below)

Status rules (mechanical, no vibes):
  FADING   : close fell back inside the box, or gain <= -5%
  TRENDING : streak >= 2, or gain >= +5% while still above the box
  WATCHING : everything else

Enrichment is fail-soft: if the price fetch fails, fields stay null and the
dashboard renders them as em-dashes. The payload always writes.
"""
import json
import os
from datetime import datetime, timezone

import pandas as pd

from analyzer import cohorts, mention_report, report
from core.company import days_to_earnings
from core.config import DASHBOARD_JSON, MARKETS
from core import deadlist
from core.daily import buyout_heuristic, daily_stats
from core.lifecycle import decide_close, walk
from core.stop_study import load as load_stop_study
from core.datafeed import fetch_history
from core.db import connect
from core.datafeed import fetch_history as _fetch_bench
from core.signals import regime as _regime
from core.stops import position_trailing_stop, watchlist_entry_stop

HISTORY_JSON = "history.json"
# Closed signals move to their own file. All of them kept in data.json would
# be ~1.1MB that the dashboard re-fetches on every load, most of it rows
# nobody is deciding anything from today.
RECENT_DAYS = 30
# Older signals are only worth fetching/showing if they're still winning.
# Bounding at 180d keeps the daily price-fetch cost sane (Darvas-style
# breakouts rarely stay meaningfully "trending" for six months, and the
# spark/box-today math wasn't validated much past that either).
# Was 180. The April cohort hit that wall in Oct 2026 with seven names up
# 50%+ still TRENDING — TEAM +130% and CORT +126% among them — and the
# dashboard would have deleted them for being old. Darvas held winners for
# over a year; a tracker for that method must not have a six-month memory.
# Cost is bounded by the TRENDING test below, not by this number: a faded
# signal is still dropped at 30 days.
EXTENDED_DAYS = 730


def _streaks(closes: pd.Series, breakout_price: float) -> tuple[int, int]:
    """(current, peak) runs of consecutive day-over-day HIGHER closes.
    Strict v1 logic: resets to 0 on ANY close that isn't strictly higher
    than the prior close. No exceptions - if yesterday was flat or down,
    streak is 0, even if price is still above the original breakout price.
    (breakout_price kept as a param for call-site compat; unused now -
    a prior version used it for a "still above entry" fallback that made
    down-days show a false streak of 1. Removed: that conflated two
    different ideas - streak and gain-since-entry - into one misleading
    number.)"""
    vals = closes.tolist()
    cur = peak = run = 0
    for i in range(1, len(vals)):
        run = run + 1 if vals[i] > vals[i - 1] else 0
        peak = max(peak, run)
    cur = run
    return cur, peak


def enrich(signals: list[dict], cfg) -> dict:
    """Returns the price frames it fetched, so the close lifecycle can walk
    the same bars rather than paying for a second download."""
    if not signals:
        return {}
    tickers = sorted({s["ticker"] + cfg.ticker_suffix for s in signals})
    try:
        # A full year, not 3 months. Two things need it: the close lifecycle
        # walks bars forward from the signal date, so a signal that stopped out
        # on day 20 of a 150-day life needs that day-20 bar to be in the frame
        # at all; and high_52w is only a 52-week high if 252 sessions are
        # actually present. A 3-month window silently gave a 63-day high and a
        # walk that began 90 days after the event it was looking for.
        # Same skip the scan applies: a delisted symbol stays in the signal
        # history forever, and re-requesting it every export only re-proves
        # that it is gone.
        tickers = deadlist.filter_live(cfg.key, tickers, "export")
        hist = fetch_history(tickers, period="1y")
        deadlist.record(cfg.key, tickers, set(hist))
    except Exception as e:                                   # noqa: BLE001
        print(f"  enrich: fetch failed entirely ({e}) — shipping nulls")
        hist = {}

    today = datetime.now(timezone.utc).date()
    for s in signals:
        s.update({"now_price": None, "gain_pct": None, "age_days": None,
                  "streak": None, "peak_streak": None,
                  "vol_today": None, "box_today": None, "status": None,
                  "box_bottom_now": None, "box_top_now": None, "box_pos": None,
                  "clean_entry": None, "awaiting_close": False,
                  "last_bar": None, "spark": []})
        s["age_days"] = (today - datetime.strptime(s["scan_date"], "%Y-%m-%d").date()).days
        df = hist.get(s["ticker"] + cfg.ticker_suffix)
        if df is None or not s.get("price") or not s.get("box_top"):
            continue
        df = df.copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        # Hand the normalised frame back, so the close lifecycle walks exactly
        # the bars that produced the numbers above rather than re-deriving them.
        hist[s["ticker"] + cfg.ticker_suffix] = df

        # A gain is only meaningful against a bar at or after the signal.
        # Intraday signals routinely flag before that day's daily bar exists,
        # and a halted or thinly-traded name can lag for days. Comparing the
        # signal price to an EARLIER bar does not understate the move, it
        # inverts it: WFF flagged at $12.29 intraday against a $2.05 prior
        # close read as -83% when the stock was actually up ~500%.
        closes = df["Close"].dropna()
        if closes.empty:
            continue
        last_bar = closes.index[-1].date()
        # Always recorded, not only when it predates the signal. The feed does
        # not always have today's bar by the time this runs, and "Now" showing
        # Friday's close with nothing to say so is how a price reads as live
        # when it is a full session old.
        s["last_bar"] = last_bar.isoformat()
        sig_date = datetime.strptime(s["scan_date"], "%Y-%m-%d").date()
        if last_bar < sig_date:
            # Everything downstream (gain, status, box_today, vol_today) would
            # be derived from a pre-signal bar, so leave them null and say why.
            s["awaiting_close"] = True
            s["last_bar"] = last_bar.isoformat()
            s["spark"] = []
            s["max_gain_pct"] = s["max_dd_pct"] = None
            continue

        now = float(closes.iloc[-1])
        s["now_price"] = round(now, 2)
        s["gain_pct"] = round((now - s["price"]) / s["price"] * 100, 2)

        # Daily statistics and the buyout shape, both from bars already in hand.
        s.update(daily_stats(df))
        s["pinned_buyout"] = buyout_heuristic(df)
        s["days_to_earnings"] = days_to_earnings(s.get("next_earnings_date"))

        since = df[df.index >= pd.Timestamp(s["scan_date"])]["Close"].dropna()
        cur, peak = _streaks(since, s["price"])
        s["streak"], s["peak_streak"] = cur, peak

        # Detail-panel fields: sparkline closes since signal + best/worst path.
        vals = [round(float(v), 2) for v in since.tolist()][-60:]
        s["spark"] = vals
        if vals:
            hi = max(vals)
            s["max_gain_pct"] = round((hi - s["price"]) / s["price"] * 100, 2)
            s["max_dd_pct"] = round((min(vals) - s["price"]) / s["price"] * 100, 2)
        else:
            s["max_gain_pct"] = s["max_dd_pct"] = None

        vols = df["Volume"].dropna()
        if len(vols) >= 21:
            s["vol_today"] = bool(float(vols.iloc[-1]) >= 2.0 * float(vols.iloc[-21:-1].mean()))
        s["box_today"] = bool(now > s["box_top"])

        # Live-entry stop + current box, for buying TODAY (not flag-time).
        box = watchlist_entry_stop(df, cfg.darvas_box_days)
        if box:
            s["box_bottom_now"] = box["box_bottom"]
            s["box_top_now"] = box["box_top"]
            s["entry_stop_now"] = box["stop"]
            if now > box["box_top"]:
                s["box_pos"] = "above"
            elif now < box["box_bottom"]:
                s["box_pos"] = "below"
            else:
                s["box_pos"] = "inside"
            s["entry_risk_now"] = (round((now - box["stop"]) / now * 100, 2)
                                   if now > box["stop"] else None)
            # A box-based stop is only actionable if it implies a sane risk.
            # Beyond ~12% the box is too tall (extended runner) — flag it
            # rather than show a number that looks like a real stop.
            s["clean_entry"] = bool(s["entry_risk_now"] is not None
                                    and s["entry_risk_now"] <= 12)
        else:
            s["box_bottom_now"] = s["box_top_now"] = s["entry_stop_now"] = None
            s["box_pos"] = s["entry_risk_now"] = None
            s["clean_entry"] = None

        # Guarded because gain_pct can legitimately be None. The old form
        # compared None with <= and, where it did get a number, inherited
        # whatever a stale bar implied -- which is how WFF came to be labelled
        # FADING on the day it ran up ~500%.
        g = s["gain_pct"]
        if g is None:
            s["status"] = None
        elif (not s["box_today"]) or g <= -5:
            s["status"] = "FADING"
        elif cur >= 2 or g >= 5:
            s["status"] = "TRENDING"
        else:
            s["status"] = "WATCHING"
    return hist


def enrich_positions(positions: list[dict], cfg) -> None:
    """Attach now_price / pl_amount / pl_pct to open positions. Fail-soft."""
    if not positions:
        return
    tickers = sorted({p["ticker"] + cfg.ticker_suffix for p in positions})
    try:
        hist = fetch_history(tickers, period="1mo")
    except Exception as e:                                   # noqa: BLE001
        print(f"  enrich_positions: fetch failed ({e}) — shipping nulls")
        hist = {}
    for p in positions:
        p.update({"now_price": None, "pl_amount": None, "pl_pct": None})
        df = hist.get(p["ticker"] + cfg.ticker_suffix)
        if df is None or not p.get("entry_price"):
            continue
        now = float(df["Close"].dropna().iloc[-1])
        p["now_price"] = round(now, 2)
        p["pl_amount"] = round((now - p["entry_price"]) * p["shares"], 2)
        p["pl_pct"] = round((now - p["entry_price"]) / p["entry_price"] * 100, 2)

        # Trailing stop: ratchet up from entry as the trade gains.
        new_stop, new_high = position_trailing_stop(
            p["entry_price"], p.get("current_stop") or p["initial_stop"],
            p.get("running_high") or p["entry_price"], now)
        p["current_stop"] = new_stop
        p["running_high"] = new_high
        p["stop_moved"] = bool(new_stop > (p.get("initial_stop") or 0))


_FLAG_FIELDS = ("hit10_before_stop", "pinned_buyout", "new_52w_high",
                "trend_pass", "vol_today", "box_today", "clean_entry")


def _coerce_flags(s: dict) -> dict:
    """SQLite stores booleans as 0/1, and a column with any NULL comes back
    from pandas as float64 -- so a re-read signal carries 0.0 where it was
    written as false. Harmless to render, but it breaks a strict `=== true`
    anywhere downstream and makes two exports of the same row differ. Restore
    the bool, leaving None as None.
    """
    for k in _FLAG_FIELDS:
        v = s.get(k)
        if v is not None and not isinstance(v, bool):
            try:
                s[k] = bool(float(v))
            except (TypeError, ValueError):
                pass
    return s


def _apply_lifecycle(signals: list[dict], hist: dict, cfg) -> None:
    """Decide which signals are now closed, and record how they ended.

    Writes close_date / close_reason / exit_price / final_return_pct /
    hit10_before_stop / hit10_date / max_gain_pct / max_dd_pct /
    peak_gain_pct / trough_dd_pct onto each closed signal, and persists the
    same to the db so the close survives the next export.

    Reaching +10% is recorded, not closed on -- see core.lifecycle for the
    measured reason why.
    """
    today = datetime.now(timezone.utc).date()
    to_persist = []
    for s in signals:
        # Already closed on a previous run: keep the stored verdict, do not
        # recompute it against newer prices.
        if s.get("close_reason"):
            continue
        df = hist.get(s["ticker"] + cfg.ticker_suffix)
        age = s.get("age_days") or 0
        w = walk(df, s["scan_date"], s.get("price"), s.get("suggested_stop"))
        # Max gain/drawdown from the walk, whatever happens below. These are
        # intraday highs and lows over the signal's whole life; enrich() set a
        # weaker version off the last 60 closes, which understates both.
        if w["max_gain_pct"] is not None:
            s["max_gain_pct"], s["max_dd_pct"] = w["max_gain_pct"], w["max_dd_pct"]
            # What the signal identified, exit ignored. This is the number that
            # shows a 312% mover was caught even when the stop took you out of
            # it early; max_gain_pct alone would report that as a small loss.
            s["peak_gain_pct"], s["trough_dd_pct"] = w["peak_gain_pct"], w["trough_dd_pct"]
        # Only answerable if the walk saw the whole life -- it is a question
        # about which of two events came first.
        s["hit10_before_stop"] = w["hit10_before_stop"] if w["walk_complete"] else None
        s["hit10_date"] = w["hit10_date"] if w["walk_complete"] else None

        verdict = decide_close(s, w, age)
        if not verdict:
            continue
        s.update(verdict)
        if not s.get("close_date"):
            s["close_date"] = today.isoformat()
        exit_px = s.get("exit_price") or s.get("now_price")
        s["exit_price"] = round(exit_px, 4) if exit_px else None
        if exit_px and s.get("price"):
            s["final_return_pct"] = round((exit_px / s["price"] - 1) * 100, 2)
        to_persist.append(s)

    if not to_persist:
        return
    with connect() as con:
        for s in to_persist:
            con.execute(
                "UPDATE signals SET close_date=?, close_reason=?, exit_price=?, "
                "final_return_pct=?, hit10_before_stop=?, hit10_date=?, "
                "max_gain_pct=?, max_dd_pct=?, peak_gain_pct=?, trough_dd_pct=?, "
                "pinned_buyout=? WHERE id=?",
                (s["close_date"], s["close_reason"], s.get("exit_price"),
                 s.get("final_return_pct"),
                 None if s.get("hit10_before_stop") is None
                 else int(s["hit10_before_stop"]),
                 s.get("hit10_date"),
                 s.get("max_gain_pct"), s.get("max_dd_pct"),
                 s.get("peak_gain_pct"), s.get("trough_dd_pct"),
                 # Persisted at close, because a settled signal is never
                 # re-priced and so never recomputes this.
                 None if s.get("pinned_buyout") is None
                 else int(s["pinned_buyout"]),
                 s["id"]))
    print(f"  closed {len(to_persist)} signal(s): "
          + ", ".join(f"{s['ticker']}({s['close_reason']})" for s in to_persist[:6])
          + ("…" if len(to_persist) > 6 else ""))


def _health(con, key: str) -> dict:
    """Did the scanner actually run, and what did it see?

    The distinction this exists to draw: a market with no breakouts writes no
    signal rows, exactly like a market whose job died. Reading MAX(scan_date)
    off signals conflates the two -- it answers "when did something last fire",
    not "when did we last look". India ran clean for five trading days in
    Sept 2026 finding nothing in a RISK-OFF tape, and a staleness warning
    built on signal dates called it broken.

    Returns {} when the runs table has no rows yet (any db written before the
    run log existed), so the dashboard stays quiet rather than inventing an
    alarm about history it cannot see.
    """
    last = con.execute(
        "SELECT run_ts, run_date, status, universe, eligible, breakouts, saved, error "
        "FROM runs WHERE market=? ORDER BY run_ts DESC LIMIT 1", (key,)).fetchone()
    if last is None:
        return {}
    out = {k: last[k] for k in last.keys()}
    # A single bad run is noise; a run of bad runs is a broken market.
    out["fails_7d"] = con.execute(
        "SELECT COUNT(*) FROM runs WHERE market=? AND status!='ok' "
        "AND julianday('now') - julianday(run_ts) <= 7", (key,)).fetchone()[0]
    out["runs_7d"] = con.execute(
        "SELECT COUNT(*) FROM runs WHERE market=? "
        "AND julianday('now') - julianday(run_ts) <= 7", (key,)).fetchone()[0]
    # What this market's universe normally looks like, so the dashboard can say
    # "48, usually 380" rather than printing a number with nothing to judge it
    # against. A collapsed universe makes a "no breakouts" result meaningless,
    # and that has to be visible without reading the Telegram digest.
    # Healthy runs only, for the same reason the scanner's check uses them:
    # a baseline that absorbs degraded runs normalises the outage away.
    vals = sorted(r[0] for r in con.execute(
        "SELECT universe FROM runs WHERE market=? AND universe IS NOT NULL "
        "AND status='ok' ORDER BY run_ts DESC LIMIT 10", (key,)))
    out["universe_typical"] = vals[len(vals) // 2] if len(vals) >= 3 else None
    return out


def export():
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {"generated": stamp, "markets": {}}
    history = {"generated": stamp, "markets": {}}
    with connect() as con:
        for key, cfg in MARKETS.items():
            sig = pd.read_sql_query(
                "SELECT s.*, o.ret_d7, o.ret_d30 FROM signals s "
                "LEFT JOIN outcomes o ON o.signal_id = s.id "
                "WHERE s.market=? AND julianday('now') - julianday(s.scan_date) <= ? "
                "ORDER BY s.scan_date DESC, s.rs_pct DESC",
                con, params=(key, EXTENDED_DAYS))
            pos = pd.read_sql_query(
                "SELECT * FROM positions WHERE market=? AND status='open'", con, params=(key,))
            signals = json.loads(sig.to_json(orient="records"))
            # Signals closed on an earlier run are settled: their verdict is
            # stored and must not be recomputed against newer prices. Holding
            # them out of enrich() also keeps the download from growing without
            # bound -- otherwise every ticker that ever fired gets re-priced on
            # every export, forever, to no purpose.
            signals = [_coerce_flags(s) for s in signals]
            hist = enrich(signals, cfg)
            # Signals are CLOSED, never deleted. The old rule dropped a
            # signal once it stopped trending, so the record quietly rewrote
            # itself to survivors only and any win/loss count taken from it
            # was wrong by construction.
            #
            # data.json carries the open ones (what you decide from today);
            # history.json carries the closed ones (what the system has
            # actually produced). Both are kept forever.
            _apply_lifecycle(signals, hist, cfg)
            closed = [s for s in signals if s.get("close_reason")]

            # The Today view keeps the ORIGINAL retention rule, and closing a
            # signal no longer removes it from there.
            #
            # Splitting them looked right -- data.json for live decisions,
            # history.json for the record -- but it hid 443 of 572 US signals
            # in one run, including 135 that had peaked +20% or better. A stock
            # that dipped through its stop once and then ran is exactly what
            # this tool is for, and filing it away as a loss is worse than the
            # survivorship problem the close lifecycle was built to fix.
            #
            # So closes are still computed and still written to history.json,
            # where honest win/loss counts come from. They just no longer
            # decide what is on screen.
            signals = [s for s in signals
                       if (s.get("age_days") or 0) <= RECENT_DAYS
                       or s.get("status") == "TRENDING"]
            history["markets"][key] = {"label": cfg.label,
                                       "currency": cfg.currency,
                                       "signals": closed}
            positions = json.loads(pos.to_json(orient="records"))
            enrich_positions(positions, cfg)
            # Persist any ratcheted stops back to the db
            for p in positions:
                if p.get("current_stop") is not None:
                    con.execute("UPDATE positions SET current_stop=?, running_high=? WHERE id=?",
                                (p["current_stop"], p.get("running_high"), p["id"]))
            # Market regime, recomputed here so it's current as of THIS export
            # (the scan may not have run for this market today). Fail-soft:
            # any fetch problem yields UNKNOWN rather than a wrong label.
            try:
                bench = _fetch_bench([cfg.benchmark], period="1y").get(cfg.benchmark)
            except Exception as e:                              # noqa: BLE001
                print(f"  regime: benchmark fetch failed ({e})")
                bench = None
            mkt_regime = _regime.evaluate(bench)
            print(f"  {key} regime: {mkt_regime['label']} ({mkt_regime['detail']})")

            payload["markets"][key] = {
                "label": cfg.label,
                "currency": cfg.currency,
                "signals": signals,
                "positions": positions,
                "edge": report(key),
                "cohorts": cohorts(key),
                "mentions": mention_report(key),
                "regime": mkt_regime,
                # Newest price bar behind every "now" number below. The
                # dashboard prints this next to the scan stamp so a stale feed
                # is visible rather than silently passed off as live.
                "bars_through": max((s.get("last_bar") for s in signals
                                     if s.get("last_bar")), default=None),
                # Computed weekly by run_stop_study.py, not here: it needs
                # price history for closed signals, which this export
                # deliberately no longer downloads. None until it first runs.
                "stop_study": load_stop_study(con, key),
                # Newest date a signal FIRED. Note this is not the same thing
                # as the last time the scanner ran -- see health below.
                "last_scan": con.execute(
                    "SELECT MAX(scan_date) FROM signals WHERE market=?", (key,)
                ).fetchone()[0],
                "health": _health(con, key),
            }
    with open(DASHBOARD_JSON, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    n_open = sum(len(m["signals"]) for m in payload["markets"].values())
    print(f"wrote {DASHBOARD_JSON} ({n_open} open signals)")

    # Closed signals, appended to whatever is already on disk so the record
    # only ever grows. Keyed by signal id, so re-exporting cannot duplicate.
    prior = {}
    if os.path.exists(HISTORY_JSON):
        try:
            with open(HISTORY_JSON) as f:
                old = json.load(f)
            for k, m in (old.get("markets") or {}).items():
                prior[k] = {s["id"]: s for s in m.get("signals", [])}
        except Exception as e:                              # noqa: BLE001
            print(f"  history: could not read existing file ({e}) — starting fresh")
    for k, m in history["markets"].items():
        merged = prior.get(k, {})
        for s in m["signals"]:
            # A signal closed on an earlier run comes back as a bare db row --
            # no sparkline, no status, none of the enrichment it carried on the
            # day it closed, because it is deliberately no longer being priced.
            # Layering it over the stored entry keeps that detail and still lets
            # the db win on the close fields themselves.
            old = merged.get(s["id"])
            merged[s["id"]] = {**old, **s} if old else s
        m["signals"] = sorted(merged.values(),
                              key=lambda x: (x.get("close_date") or "", x["ticker"]))
    with open(HISTORY_JSON, "w") as f:
        json.dump(history, f, separators=(",", ":"))
    n_closed = sum(len(m["signals"]) for m in history["markets"].values())
    print(f"wrote {HISTORY_JSON} ({n_closed} closed signals)")


if __name__ == "__main__":
    export()
