"""
Trend filter: is this stock even eligible to be bought?
Four mechanical checks (O'Neil / Minervini trend template, reduced to essentials):

  1. price > 50-day MA
  2. 50-day MA > 200-day MA
  3. 200-day MA rising (vs 21 sessions ago)
  4. price within `near_high_pct` of its 52-week high

A breakout in a stock that fails this gate is noise, not signal.
"""
import pandas as pd


def evaluate(df: pd.DataFrame, near_high_pct: float = 25.0) -> dict:
    close = df["Close"].dropna()
    if len(close) < 210:
        # Not enough history for MA200 — fail closed, say why.
        return {"pass": False, "checks": {}, "detail": "insufficient history (<210d)",
                "dist_52w_high": None}

    price = float(close.iloc[-1])
    ma50 = float(close.rolling(50).mean().iloc[-1])
    ma200_series = close.rolling(200).mean()
    ma200 = float(ma200_series.iloc[-1])
    ma200_prev = float(ma200_series.iloc[-21])
    high_52w = float(close.tail(252).max())
    dist_high = round((high_52w - price) / high_52w * 100, 2)

    checks = {
        "above_ma50":   price > ma50,
        "ma50_gt_ma200": ma50 > ma200,
        "ma200_rising": ma200 > ma200_prev,
        "near_52w_high": dist_high <= near_high_pct,
    }
    passed = sum(checks.values())
    return {
        "pass": passed == 4,
        "checks": checks,
        "detail": f"{passed}/4: " + ",".join(k for k, v in checks.items() if v),
        "dist_52w_high": dist_high,
    }
