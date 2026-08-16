"""
Market regime: is the market itself supporting breakouts right now?

Why this exists
---------------
Every ASR filter asks "is this STOCK good?" — none asked "is this a good WEEK
to be buying breakouts at all?" Measured on this repo's own US signals, the
same unchanged rules produced:

    2026-04   n=67   avg 30d  +9.33%   hit 53.7%
    2026-05   n=121  avg 30d  -0.80%   hit 50.4%
    2026-06   n=44   avg 30d  -9.64%   hit 40.9%
    2026-07   n=22   avg 30d -10.22%   hit 31.8%

Same code, ~20pp swing. That spread is the market, not the filters.

The read (three mechanical checks on the market's own index)
------------------------------------------------------------
  1. index > its own 200-day MA      (long-term uptrend intact)
  2. index > its own 50-day MA       (short-term not broken)
  3. 50-day MA > 200-day MA          (the two agree)

  3/3 -> RISK-ON      breakouts have the wind behind them
  2/3 -> MIXED        tradeable, but expect more failures
  0-1  -> RISK-OFF    breakouts historically weakest; size down or sit out

This uses the SAME index already downloaded each run for relative strength
(SPY / ^NSEI / ^GSPTSE), so it costs one extra computation on data that is
already in memory — no new fetch, no new dependency, no new failure mode.

IMPORTANT — this is INFORMATION, NOT A FILTER.
It does not block, rank, or score a single signal. Nothing about which stocks
appear changes. It only labels the conditions they appeared in, so the label
can be measured against real outcomes for a few months before anyone considers
letting it gate anything. Wiring an unmeasured rule into the pipeline is how
the old grading system went wrong.
"""
import pandas as pd

LABELS = {3: "RISK-ON", 2: "MIXED"}


def evaluate(bench_df: pd.DataFrame | None) -> dict:
    """Read the market regime off the benchmark index. Fails soft: if the
    benchmark fetch failed or history is short, returns UNKNOWN rather than
    guessing, so a bad data day can't silently mislabel the conditions."""
    if bench_df is None or "Close" not in bench_df:
        return {"label": "UNKNOWN", "score": None, "detail": "benchmark unavailable",
                "checks": {}, "pct_from_200ma": None}

    close = bench_df["Close"].dropna()
    if len(close) < 210:
        return {"label": "UNKNOWN", "score": None, "detail": "insufficient index history",
                "checks": {}, "pct_from_200ma": None}

    price = float(close.iloc[-1])
    ma50 = float(close.rolling(50).mean().iloc[-1])
    ma200 = float(close.rolling(200).mean().iloc[-1])

    checks = {
        "index_above_ma200": price > ma200,
        "index_above_ma50": price > ma50,
        "ma50_gt_ma200": ma50 > ma200,
    }
    score = sum(checks.values())
    return {
        "label": LABELS.get(score, "RISK-OFF"),
        "score": score,
        "detail": f"{score}/3: " + (",".join(k for k, v in checks.items() if v) or "none"),
        "checks": checks,
        "pct_from_200ma": round((price - ma200) / ma200 * 100, 2),
    }
