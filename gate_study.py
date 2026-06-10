"""
Retroactive trend-gate study.

Question: would the v2 trend gate (price > MA50, MA50 > MA200, MA200 rising,
within 25% of 52w high) have improved the historical signal set?

Method: for every signal with a measured outcome, slice that ticker's price
history to END on the alert date — no lookahead — run the gate exactly as the
scanner does, then compare outcomes: gated-pass vs gated-fail.

Run from the repo root:
    python3 gate_study.py US
"""
import sys

import pandas as pd

from core.config import MARKETS
from core.datafeed import fetch_history
from core.db import connect
from core.signals import trend_filter


def study(market_key: str = "US"):
    cfg = MARKETS[market_key]
    with connect() as con:
        df = pd.read_sql_query(
            "SELECT s.id, s.ticker, s.scan_date, s.price, s.legacy_grade, "
            "o.ret_d7, o.ret_d14, o.ret_d30, o.stop_hit_d30 "
            "FROM signals s JOIN outcomes o ON o.signal_id = s.id "
            "WHERE s.market=? AND o.ret_d14 IS NOT NULL", con, params=(market_key,))
    if df.empty:
        print("No measured signals to study."); return

    tickers = sorted(df["ticker"].unique())
    print(f"Replaying gate for {len(df)} signals across {len(tickers)} tickers "
          f"(fetching 2y history)...")
    hist = fetch_history([t + cfg.ticker_suffix for t in tickers], period="2y")

    verdicts = []
    for _, s in df.iterrows():
        h = hist.get(s["ticker"] + cfg.ticker_suffix)
        if h is None:
            verdicts.append(None); continue
        h = h.copy(); h.index = pd.to_datetime(h.index).tz_localize(None)
        asof = h[h.index <= pd.Timestamp(s["scan_date"])]   # nothing after alert date
        res = trend_filter.evaluate(asof, cfg.trend_near_high_pct)
        verdicts.append(bool(res["pass"]))
    df["gate"] = verdicts
    df = df.dropna(subset=["gate"])

    def block(name, d):
        if d.empty:
            print(f"\n{name}: n=0"); return
        r30 = d["ret_d30"].dropna()
        line = (f"\n{name}  (n={len(d)})\n"
                f"  14d: hit {100*(d['ret_d14']>0).mean():.1f}%  avg {d['ret_d14'].mean():+.2f}%\n")
        if len(r30):
            line += (f"  30d: hit {100*(r30>0).mean():.1f}%  avg {r30.mean():+.2f}%  "
                     f"median {r30.median():+.2f}%  (n={len(r30)})\n")
        sh = d["stop_hit_d30"].dropna()
        if len(sh):
            line += f"  stops hit within 30d: {100*sh.mean():.1f}%"
        print(line)

    print("\n" + "=" * 58)
    print(f"TREND GATE STUDY — {cfg.label} — {len(df)} signals replayed")
    print("=" * 58)
    block("GATE PASS  (what v2 would have alerted)", df[df["gate"] == True])    # noqa: E712
    block("GATE FAIL  (what v2 would have killed)", df[df["gate"] == False])    # noqa: E712
    block("ALL        (the v1 firehose, for reference)", df)

    p, f = df[df["gate"] == True], df[df["gate"] == False]                      # noqa: E712
    if len(p) >= 10 and len(f) >= 10:
        diff = p["ret_d30"].dropna().mean() - f["ret_d30"].dropna().mean()
        print("\n" + "-" * 58)
        print(f"VERDICT: gate-pass signals outperformed gate-fail by "
              f"{diff:+.2f}pp on 30d average.")
        print("Positive and meaningful (>3pp) = the gate is real edge added.")
        print("Near zero or negative = the gate only reduces noise, not improves it.")
    else:
        print("\nNote: one group has <10 signals — read the comparison as "
              "directional, not conclusive.")


if __name__ == "__main__":
    study(sys.argv[1] if len(sys.argv) > 1 else "US")
