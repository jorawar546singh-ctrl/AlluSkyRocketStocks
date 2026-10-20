"""
What would a different stop have done?

The live rule puts the stop just under the Darvas box bottom and never moves
it. Replay every closed signal's actual bars against other stop rules, both
fixed and trailing, and report what each would have produced.

Why trailing rules are here
---------------------------
A fixed stop below the entry price cannot produce a winning exit. The only
ways a position closes are: fall back to that stop (a loss by definition),
expire, or fade. Measured on the real history, 100% of signals that reached
+10% still closed negative -- as did 100% of those that reached +50%, and
100% of those that doubled. That is not a statistic about the strategy, it is
an arithmetic consequence of the exit design, and it makes the entry's quality
unmeasurable: you cannot detect edge through an exit incapable of winning.

A trailing stop ratchets upward as the position gains, so a move that goes
+50% and gives half of it back still books a profit. That is the mechanism
being tested.

Intraday ordering
-----------------
Daily bars do not say whether the high or the low came first, so two rules
keep this honest:

  * The stop is checked BEFORE it is ratcheted by the same bar's high. A
    position cannot be stopped out at a level that only this bar's high
    created.
  * The signal day itself is skipped entirely (see lifecycle.walk): a Darvas
    entry happens intraday on a break above the box, and that bar's low
    usually predates the entry.

On reading these results
------------------------
Nine variants over one dataset is nine chances for noise to look like signal.
The levels are deliberately not searched for -- round percentages, breakeven,
and the strategy's own Darvas box -- but picking the best row afterwards is
still selection. The t column is avg/(sd/sqrt(n)); below 2 the average is not
distinguishable from zero whatever its sign.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

from core.lifecycle import walk

TARGET_PCT = 10.0
DARVAS_DAYS = 14

# (label, kind, params). kind "fixed" never moves the stop; "trail" ratchets.
VARIANTS: list[tuple[str, str, dict]] = [
    ("5% below entry",        "fixed", {"init": "pct:5"}),
    ("7% below entry",        "fixed", {"init": "pct:7"}),
    ("10% below entry",       "fixed", {"init": "pct:10"}),
    ("under box bottom",      "fixed", {"init": "box"}),
    ("as signalled",          "fixed", {"init": "signalled"}),
    ("breakeven at +10%",     "trail", {"init": "signalled", "breakeven_at": 10.0}),
    ("trail 10% below high",  "trail", {"init": "signalled", "trail_pct": 10.0}),
    ("trail 20% below high",  "trail", {"init": "signalled", "trail_pct": 20.0}),
    ("trail 14d Darvas low",  "trail", {"init": "signalled", "darvas_days": DARVAS_DAYS}),
]


def _initial_stop(spec: str, s: dict) -> float | None:
    entry = s.get("price")
    if not entry:
        return None
    if spec.startswith("pct:"):
        return entry * (1 - float(spec.split(":")[1]) / 100)
    if spec == "box":
        bb = s.get("box_bottom")
        return bb * 0.995 if bb else None     # a stop AT the bottom is hit by every probe
    if spec == "signalled":
        return s.get("suggested_stop")
    return None


def _frame_after(df: pd.DataFrame, scan_date: str):
    d = df.dropna(subset=["High", "Low", "Close"])
    if getattr(d.index, "tz", None) is not None:
        d = d.copy()
        d.index = d.index.tz_localize(None)
    return d


def simulate(df, scan_date, entry, init_stop, *, trail_pct=None,
             breakeven_at=None, darvas_days=None, target_pct=TARGET_PCT) -> dict | None:
    """Replay one signal under one stop rule.

    Returns {ret_pct, stopped, hit10_before_stop, exit_date} or None when the
    bars cannot support a walk.
    """
    if df is None or df.empty or not entry or entry <= 0:
        return None
    d = _frame_after(df, scan_date)
    sig = pd.Timestamp(scan_date)
    # The rolling Darvas low is computed on the FULL frame so the window at the
    # first post-signal bar still looks back over real prior bars.
    roll_low = (d["Low"].rolling(darvas_days).min() if darvas_days else None)
    d2 = d[d.index > sig]
    if d2.empty:
        return None

    stop = init_stop
    high = entry
    target = entry * (1 + target_pct / 100)
    hit10 = None

    for ts, bar in d2.iterrows():
        lo, hi = float(bar["Low"]), float(bar["High"])

        # 1. Stop first, against the level as it stood BEFORE this bar.
        if stop is not None and lo <= stop:
            return {"ret_pct": (stop / entry - 1) * 100, "stopped": True,
                    "hit10_before_stop": bool(hit10) if hit10 is not None else False,
                    "exit_date": ts.strftime("%Y-%m-%d")}

        # 2. The +10% milestone, recorded but never an exit.
        if hit10 is None and hi >= target:
            hit10 = True

        # 3. Ratchet, using this bar's high. The stop never moves down.
        high = max(high, hi)
        new = stop
        if breakeven_at is not None and high >= entry * (1 + breakeven_at / 100):
            new = entry if new is None else max(new, entry)
        if trail_pct is not None:
            cand = high * (1 - trail_pct / 100)
            new = cand if new is None else max(new, cand)
        if darvas_days is not None and roll_low is not None:
            rl = roll_low.get(ts)
            if rl is not None and pd.notna(rl):
                new = float(rl) if new is None else max(new, float(rl))
        stop = new

    # Never stopped: held to the end of the available window.
    last = float(d2["Close"].iloc[-1])
    return {"ret_pct": (last / entry - 1) * 100, "stopped": False,
            "hit10_before_stop": bool(hit10) if hit10 is not None else False,
            "exit_date": d2.index[-1].strftime("%Y-%m-%d")}


def _stats(vals: list[float]) -> dict:
    n = len(vals)
    if not n:
        return {"n": 0, "avg": None, "median": None, "sd": None, "t": None}
    avg = sum(vals) / n
    sv = sorted(vals)
    med = sv[n // 2] if n % 2 else (sv[n // 2 - 1] + sv[n // 2]) / 2
    sd = (sum((v - avg) ** 2 for v in vals) / (n - 1)) ** 0.5 if n > 1 else None
    t = (avg / (sd / n ** 0.5)) if (sd and sd > 0) else None
    return {"n": n, "avg": round(avg, 2), "median": round(med, 2),
            "sd": round(sd, 2) if sd else None,
            "t": round(t, 2) if t is not None else None}


def run(signals: list[dict], frames: dict, suffix: str = "") -> dict:
    out = {"computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "target_pct": TARGET_PCT, "variants": [], "n_signals": len(signals),
           "n_walkable": 0}

    usable = []
    for s in signals:
        df = frames.get(s["ticker"] + suffix)
        if df is None or df.empty or not s.get("price"):
            continue
        # Only signals whose bars reach back to the signal date can answer an
        # order-of-events question. See lifecycle.walk_complete.
        if not walk(df, s["scan_date"], s["price"], None, TARGET_PCT)["walk_complete"]:
            continue
        usable.append((s, df))
    out["n_walkable"] = len(usable)

    for label, kind, params in VARIANTS:
        rets, hit10, stopped, skipped = [], [], 0, 0
        for s, df in usable:
            init = _initial_stop(params["init"], s)
            if init is None:
                skipped += 1
                continue
            kw = {k: v for k, v in params.items() if k != "init"}
            r = simulate(df, s["scan_date"], s["price"], init, **kw)
            if r is None:
                skipped += 1
                continue
            rets.append(r["ret_pct"])
            hit10.append(r["hit10_before_stop"])
            stopped += int(r["stopped"])
        n = len(rets)
        st = _stats(rets)
        out["variants"].append({
            "label": label,
            "kind": kind,
            "n": n,
            "skipped": skipped,
            "hit10_rate": round(100 * sum(hit10) / n, 1) if n else None,
            "stop_rate": round(100 * stopped / n, 1) if n else None,
            # Share of signals that actually closed in profit. Under every
            # fixed-stop rule this is near zero by construction, which is the
            # whole reason the trailing variants exist.
            "win_rate": round(100 * sum(1 for v in rets if v > 0) / n, 1) if n else None,
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
