"""
Per-signal daily statistics, computed from bars the scan already has.

No network. Everything here is derived from the same daily DataFrame the
trend gate and Darvas trigger already read, so adding these costs nothing
per run.

Two of these replace weaker versions already in the payload:
  vol_ratio_50d  the existing vol_ratio is against a 20-day average, which is
                 short enough that one busy week moves the baseline. The 50-day
                 version is steadier; both are kept so nothing downstream breaks.
  dist_52w_high_pct  the trend gate's dist_52w_high is computed over the gate's
                 own lookback. This one is an honest 252-session window.
"""
from __future__ import annotations

import pandas as pd

SESSIONS_52W = 252
BUYOUT_RANGE_PCT = 2.0      # a pending cash deal pins the price in a tight band
BUYOUT_MIN_DAYS = 10
BUYOUT_GAP_PCT = 20.0


def daily_stats(df: pd.DataFrame) -> dict:
    """prev_close, close, day_change_pct, volume, avg_volume_50d,
    vol_ratio_50d, high_52w, dist_52w_high_pct, new_52w_high.

    Returns whatever it can; any field it cannot compute comes back None
    rather than raising, because a short history must not cost a signal.
    """
    out = {k: None for k in (
        "prev_close", "close", "day_change_pct", "volume", "avg_volume_50d",
        "vol_ratio_50d", "high_52w", "dist_52w_high_pct", "new_52w_high")}
    if df is None or df.empty:
        return out
    d = df.dropna(subset=["Close"])
    if d.empty:
        return out

    close = float(d["Close"].iloc[-1])
    out["close"] = round(close, 4)
    if len(d) >= 2:
        prev = float(d["Close"].iloc[-2])
        out["prev_close"] = round(prev, 4)
        if prev:
            out["day_change_pct"] = round((close / prev - 1) * 100, 2)

    if "Volume" in d:
        v = d["Volume"].dropna()
        if len(v):
            out["volume"] = int(v.iloc[-1])
            # Excludes today, so a huge session does not inflate its own baseline.
            window = v.iloc[-51:-1]
            if len(window) >= 20:
                avg = float(window.mean())
                out["avg_volume_50d"] = int(avg)
                if avg > 0:
                    out["vol_ratio_50d"] = round(float(v.iloc[-1]) / avg, 2)

    if "High" in d:
        h = d["High"].dropna()
        if len(h):
            prior = h.iloc[-SESSIONS_52W:-1]        # excludes today
            if len(prior):
                hi = float(prior.max())
                out["high_52w"] = round(hi, 4)
                if hi > 0:
                    out["dist_52w_high_pct"] = round((close / hi - 1) * 100, 2)
                out["new_52w_high"] = bool(float(h.iloc[-1]) >= hi)
    return out


def buyout_heuristic(df: pd.DataFrame) -> bool:
    """Tight range at the 52-week high following a large gap up.

    This is the SHAPE a pending cash acquisition leaves on a chart: the stock
    gaps to near the offer price and then stops moving, because there is
    nothing left to discover. It is a pattern match, not a fact -- a real
    acquisition flag needs a corporate-actions feed, which this project does
    not have. A stock can print this shape without any deal.

    Conditions, all required:
      * a single session gapped up >= 20% within the last 60 sessions
      * every session since has held inside a 2% band
      * that band sits at the 52-week high
    """
    if df is None or df.empty:
        return False
    d = df.dropna(subset=["Close", "High", "Low"])
    if len(d) < BUYOUT_MIN_DAYS + 5:
        return False

    closes = d["Close"]
    gaps = closes.pct_change() * 100
    recent = gaps.iloc[-60:]
    big = [i for i, g in zip(recent.index, recent.values) if g >= BUYOUT_GAP_PCT]
    if not big:
        return False

    after = d.loc[big[-1]:]
    if len(after) < BUYOUT_MIN_DAYS:
        return False

    lo, hi = float(after["Low"].min()), float(after["High"].max())
    if lo <= 0 or (hi / lo - 1) * 100 > BUYOUT_RANGE_PCT:
        return False

    # And it has to be sitting at the highs, not in a quiet patch anywhere.
    year_high = float(d["High"].iloc[-SESSIONS_52W:].max())
    return bool(year_high > 0 and (hi / year_high) >= 0.98)
