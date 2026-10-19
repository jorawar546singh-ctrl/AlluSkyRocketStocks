"""
What would a different stop have done?

The live rule puts the stop just under the Darvas box bottom. That is one
choice out of many, and the cost of it has never been measured -- so this
replays every closed signal's actual bars against several stop levels and
reports what each would have produced.

The question asked is "what share reached +10% before the stop was touched",
which is the order-of-events question walk() already answers. But that number
ALONE is misleading, and dangerously so: a looser stop raises it mechanically
(there is simply more room before you are taken out) while making every trade
that never recovers lose more. So each variant also reports the average return
it would have produced, which is the number that actually decides anything.

Exit rule used for the return, matching how the system now behaves: exit at
the stop if it is touched, otherwise hold to the end of the tracking window.
Reaching +10% does NOT exit -- see core.lifecycle.CLOSE_ON_TARGET.

On reading these results
------------------------
Five variants over one dataset is five chances for noise to look like signal.
The levels here are deliberately NOT searched for: 5/7/10% are conventional
round numbers and the box bottom is the strategy's own rule, so none of them
were chosen by looking at the answer first. A difference of a point or two
between adjacent variants is noise. The t column is avg/(sd/sqrt(n)); below
2 the average is not distinguishable from zero whatever its sign.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

from core.lifecycle import walk

TARGET_PCT = 10.0
# (label, how to derive the stop price from the signal row)
VARIANTS: list[tuple[str, str]] = [
    ("5% below entry", "pct:5"),
    ("7% below entry", "pct:7"),
    ("10% below entry", "pct:10"),
    ("under box bottom", "box"),
    ("as signalled", "signalled"),
]


def _stop_for(spec: str, s: dict) -> float | None:
    entry = s.get("price")
    if not entry:
        return None
    if spec.startswith("pct:"):
        return entry * (1 - float(spec.split(":")[1]) / 100)
    if spec == "box":
        # The strategy's own rule: just under the box bottom. Slightly under,
        # because a stop exactly at the bottom is hit by every probe of it.
        bb = s.get("box_bottom")
        return bb * 0.995 if bb else None
    if spec == "signalled":
        return s.get("suggested_stop")
    return None


def _stats(vals: list[float]) -> dict:
    n = len(vals)
    if not n:
        return {"n": 0, "avg": None, "median": None, "sd": None, "t": None}
    avg = sum(vals) / n
    med = sorted(vals)[n // 2] if n % 2 else sum(sorted(vals)[n // 2 - 1:n // 2 + 1]) / 2
    sd = (sum((v - avg) ** 2 for v in vals) / (n - 1)) ** 0.5 if n > 1 else None
    t = (avg / (sd / n ** 0.5)) if (sd and sd > 0) else None
    return {"n": n, "avg": round(avg, 2), "median": round(med, 2),
            "sd": round(sd, 2) if sd else None,
            "t": round(t, 2) if t is not None else None}


def run(signals: list[dict], frames: dict, suffix: str = "") -> dict:
    """Replay `signals` against every stop variant.

    signals : rows carrying ticker, scan_date, price, box_bottom, suggested_stop
    frames  : {ticker+suffix -> daily OHLC DataFrame} covering each signal's life
    """
    out = {"computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "target_pct": TARGET_PCT, "variants": [], "n_signals": 0,
           "n_walkable": 0}

    usable = []
    for s in signals:
        df = frames.get(s["ticker"] + suffix)
        if df is None or df.empty or not s.get("price"):
            continue
        # Only signals whose bars actually reach back to the signal date can
        # answer an order-of-events question. See lifecycle.walk_complete.
        probe = walk(df, s["scan_date"], s["price"], None, TARGET_PCT)
        if not probe["walk_complete"]:
            continue
        usable.append((s, df))
    out["n_signals"] = len(signals)
    out["n_walkable"] = len(usable)

    for label, spec in VARIANTS:
        hit10, rets, stopped, skipped = [], [], 0, 0
        for s, df in usable:
            stop = _stop_for(spec, s)
            if stop is None:
                skipped += 1
                continue
            w = walk(df, s["scan_date"], s["price"], stop, TARGET_PCT)
            hit10.append(bool(w["hit10_before_stop"]))
            if w["hit_stop"]:
                stopped += 1
                rets.append((w["exit_price"] / s["price"] - 1) * 100)
            else:
                # Not stopped: held to the end of the available window.
                d = df.dropna(subset=["Close"])
                if getattr(d.index, "tz", None) is not None:
                    d = d.copy()
                    d.index = d.index.tz_localize(None)
                d = d[d.index >= pd.Timestamp(s["scan_date"])]
                if len(d):
                    rets.append((float(d["Close"].iloc[-1]) / s["price"] - 1) * 100)
        n = len(hit10)
        st = _stats(rets)
        out["variants"].append({
            "label": label,
            "n": n,
            "skipped": skipped,
            "hit10_rate": round(100 * sum(hit10) / n, 1) if n else None,
            "stop_rate": round(100 * stopped / n, 1) if n else None,
            "avg_return": st["avg"],
            "median_return": st["median"],
            "sd": st["sd"],
            "t": st["t"],
        })
    return out


def save(con, market: str, payload: dict) -> None:
    con.execute(
        "INSERT INTO studies (market, kind, computed_at, payload) VALUES (?,?,?,?) "
        "ON CONFLICT(market, kind) DO UPDATE SET computed_at=excluded.computed_at, "
        "payload=excluded.payload",
        (market, "stops", payload["computed_at"], json.dumps(payload)))


def load(con, market: str) -> dict | None:
    r = con.execute("SELECT payload FROM studies WHERE market=? AND kind='stops'",
                    (market,)).fetchone()
    if not r:
        return None
    try:
        return json.loads(r["payload"])
    except (ValueError, TypeError):
        return None
