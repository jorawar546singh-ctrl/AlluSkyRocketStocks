"""
Export data.json — the single payload the static dashboard reads.

v2.1: each recent signal is enriched with live tracking data so the
dashboard can show the v1 watchlist columns:

  now_price, gain_pct      current close, % since breakout print
  age_days                 calendar days since signal
  streak / peak_streak     consecutive closes above box top (current / best)
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
from datetime import datetime, timezone

import pandas as pd

from analyzer import report
from core.config import DASHBOARD_JSON, MARKETS
from core.datafeed import fetch_history
from core.db import connect

RECENT_DAYS = 30


def _streaks(closes: pd.Series, box_top: float) -> tuple[int, int]:
    """(current, peak) runs of consecutive closes above box_top."""
    cur = peak = run = 0
    for c in closes:
        run = run + 1 if c > box_top else 0
        peak = max(peak, run)
    cur = run
    return cur, peak


def enrich(signals: list[dict], cfg) -> None:
    if not signals:
        return
    tickers = sorted({s["ticker"] + cfg.ticker_suffix for s in signals})
    try:
        hist = fetch_history(tickers, period="3mo")
    except Exception as e:                                   # noqa: BLE001
        print(f"  enrich: fetch failed entirely ({e}) — shipping nulls")
        hist = {}

    today = datetime.now(timezone.utc).date()
    for s in signals:
        s.update({"now_price": None, "gain_pct": None, "age_days": None,
                  "streak": None, "peak_streak": None,
                  "vol_today": None, "box_today": None, "status": None})
        s["age_days"] = (today - datetime.strptime(s["scan_date"], "%Y-%m-%d").date()).days
        df = hist.get(s["ticker"] + cfg.ticker_suffix)
        if df is None or not s.get("price") or not s.get("box_top"):
            continue
        df = df.copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)

        now = float(df["Close"].dropna().iloc[-1])
        s["now_price"] = round(now, 2)
        s["gain_pct"] = round((now - s["price"]) / s["price"] * 100, 2)

        since = df[df.index >= pd.Timestamp(s["scan_date"])]["Close"].dropna()
        cur, peak = _streaks(since, s["box_top"])
        s["streak"], s["peak_streak"] = cur, peak

        vols = df["Volume"].dropna()
        if len(vols) >= 21:
            s["vol_today"] = bool(float(vols.iloc[-1]) >= 2.0 * float(vols.iloc[-21:-1].mean()))
        s["box_today"] = bool(now > s["box_top"])

        if (not s["box_today"]) or s["gain_pct"] <= -5:
            s["status"] = "FADING"
        elif cur >= 2 or s["gain_pct"] >= 5:
            s["status"] = "TRENDING"
        else:
            s["status"] = "WATCHING"


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


def export():
    payload = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "markets": {}}
    with connect() as con:
        for key, cfg in MARKETS.items():
            sig = pd.read_sql_query(
                "SELECT s.*, o.ret_d7, o.ret_d30 FROM signals s "
                "LEFT JOIN outcomes o ON o.signal_id = s.id "
                "WHERE s.market=? AND julianday('now') - julianday(s.scan_date) <= ? "
                "ORDER BY s.scan_date DESC, s.rs_pct DESC",
                con, params=(key, RECENT_DAYS))
            pos = pd.read_sql_query(
                "SELECT * FROM positions WHERE market=? AND status='open'", con, params=(key,))
            signals = json.loads(sig.to_json(orient="records"))
            enrich(signals, cfg)
            positions = json.loads(pos.to_json(orient="records"))
            enrich_positions(positions, cfg)
            payload["markets"][key] = {
                "label": cfg.label,
                "currency": cfg.currency,
                "signals": signals,
                "positions": positions,
                "edge": report(key),
            }
    with open(DASHBOARD_JSON, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"wrote {DASHBOARD_JSON}")


if __name__ == "__main__":
    export()
