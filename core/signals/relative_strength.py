"""
Relative strength: rank each candidate's trailing return against
(a) every other ticker scanned today, and (b) the market benchmark.

rs_pct    : percentile 0-100 within today's scan universe (100 = strongest)
rs_excess : ticker's lookback return minus benchmark return, in pct points
"""
import pandas as pd


def lookback_return(df: pd.DataFrame, days: int) -> float | None:
    close = df["Close"].dropna()
    if len(close) < days + 1:
        return None
    start, end = float(close.iloc[-(days + 1)]), float(close.iloc[-1])
    if start <= 0:
        return None
    return (end - start) / start * 100


def rank(histories: dict[str, pd.DataFrame], benchmark_df: pd.DataFrame | None,
         days: int = 63) -> dict[str, dict]:
    returns = {}
    for t, df in histories.items():
        r = lookback_return(df, days)
        if r is not None:
            returns[t] = r
    if not returns:
        return {}

    bench_ret = lookback_return(benchmark_df, days) if benchmark_df is not None else None
    ordered = sorted(returns.values())
    n = len(ordered)

    out = {}
    for t, r in returns.items():
        below = sum(1 for x in ordered if x < r)
        out[t] = {
            "rs_pct": round(below / max(n - 1, 1) * 100, 1),
            "rs_excess": round(r - bench_ret, 2) if bench_ret is not None else None,
            "ret_lookback": round(r, 2),
        }
    return out
