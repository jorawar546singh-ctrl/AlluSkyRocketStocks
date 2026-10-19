"""
Closing a signal instead of deleting it.

The old rule silently dropped a signal once it stopped trending or aged out,
which meant the record of what the system had actually produced quietly
rewrote itself: only survivors remained, so any count of wins and losses taken
from the dashboard was wrong by construction.

Now a signal is closed and kept. Four reasons, checked in this order because
the earlier ones are more specific:

  hit_target_10  reached +10% from the signal price
  stopped_out    traded at or below the suggested stop
  expired        passed the holding horizon without doing either
  faded          fell back inside the box, or is down 5%+, while still young

hit10_before_stop records which came FIRST, walking the bars in order. That
is the question a target-versus-stop rule actually turns on, and it cannot be
answered from max_gain and max_drawdown alone: a signal can show both +18%
and -9% over a month with no way to tell, from those two numbers, whether you
would have taken the profit or the loss.
"""
from __future__ import annotations

import pandas as pd

TARGET_PCT = 10.0
EXPIRE_DAYS = 180

# Reaching +10% is RECORDED but does not close the signal, and the measured
# reason is the whole reason:
#
#   58% of signals touch +10% within 180 days. Of those, the average move went
#   on to offer +38%, and 15 of them reached +100% or more -- HCWB +759%,
#   MRNA +337%, WFF +312%.
#
# Closing at the target files every one of those as a +10% win and stops
# watching, which is the precise failure the close lifecycle was built to end:
# a record that cannot show you a 300% move the scanner actually caught.
# The milestone is kept as hit10_date / hit10_before_stop, which costs nothing
# and answers the target-versus-stop question on its own.
#
# Set True to go back to closing at the target.
CLOSE_ON_TARGET = False


def walk(df: pd.DataFrame, scan_date: str, entry: float, stop: float | None,
         target_pct: float = TARGET_PCT) -> dict:
    """Walk bars in order from the signal and report what happened first.

    Returns {hit_target, hit_stop, hit10_before_stop, hit10_date,
             first_event_date, exit_price, max_gain_pct, max_dd_pct,
             peak_gain_pct, trough_dd_pct, walk_complete}.

    Two pairs of extremes, because they answer different questions and
    conflating them is how a 312% move gets filed as a 7% loss:

      max_gain_pct / max_dd_pct     while the signal was LIVE, up to the exit
                                    bar. What the trade itself would have been.
      peak_gain_pct / trough_dd_pct over the whole tracking window, exit
                                    ignored. What the SCANNER identified.

    WFF is the case in point: flagged before a 312% run, and a tight stop would
    have been touched on the way. One number says the trade lost; the other
    says the signal was right and the stop was too tight. Only both together
    tell you which to fix.

    Within a single bar the order of the high and the low is unknowable from
    daily data. When one bar touches both the target and the stop, the stop is
    assumed to have come first -- the pessimistic reading, so this never
    reports a profit the day's range cannot prove.

    walk_complete says whether the frame actually reached back to the signal.
    Price history is fetched for a fixed window, so an old signal can arrive
    with its first months missing -- and this function's entire job is reporting
    which event came FIRST, which is exactly the question those missing bars
    answer. Without them a stop touched in week three is invisible and a later
    recovery through the target reads as a clean win. Everything order-dependent
    downstream is gated on this flag; max_gain/max_dd are still reported, since
    a partial window understates them rather than inverting them.
    """
    out = {"hit_target": False, "hit_stop": False, "hit10_before_stop": None,
           "hit10_date": None, "first_event_date": None, "exit_price": None,
           "max_gain_pct": None, "max_dd_pct": None,
           "peak_gain_pct": None, "trough_dd_pct": None, "walk_complete": False}
    if df is None or df.empty or not entry or entry <= 0:
        return out
    d = df.dropna(subset=["High", "Low", "Close"])
    # The feed hands back a tz-aware index for some markets and a naive one for
    # others. scan_date is a plain calendar date, and comparing the two raises
    # rather than returning False, so flatten first.
    if getattr(d.index, "tz", None) is not None:
        d = d.copy()
        d.index = d.index.tz_localize(None)
    sig = pd.Timestamp(scan_date)
    # Allow a few days of slack: the signal date is a calendar date and the
    # first bar at or after it may be days later over a long weekend or a halt.
    first_bar = d.index.min()
    out["walk_complete"] = bool(first_bar is not None and first_bar <= sig + pd.Timedelta(days=5))
    # Strictly AFTER the signal day, not from it.
    #
    # A Darvas signal fires intraday when price breaks ABOVE the box top, and
    # the entry is that breakout price. But the same daily bar usually OPENED
    # inside or below the box -- which is exactly where the stop sits. Starting
    # the walk at the signal day therefore reads that bar's low, which occurred
    # BEFORE the entry, as an immediate stop-out at a price the position was
    # never in. It is not a rare edge case: it marked 54% of every stop-out in
    # the database, and it is near-guaranteed by the shape of a breakout.
    # WFF was the extreme -- entry 12.29, stop 2.83, "stopped out" the same day
    # for -77%, from a low printed before the breakout it was signalled on.
    #
    # Daily bars cannot say what the range was after an intraday entry, so the
    # signal day is skipped entirely for both the stop and the target. That is
    # mildly optimistic -- a genuine same-day reversal is missed -- but it is
    # the standard treatment and far less wrong than inventing exits.
    d = d[d.index > sig]
    if d.empty:
        return out

    target = entry * (1 + target_pct / 100)
    best, worst = entry, entry          # live: frozen at the exit bar
    peak, trough = entry, entry         # full window: keeps going
    exited = False

    for ts, bar in d.iterrows():
        hi, lo = float(bar["High"]), float(bar["Low"])
        peak, trough = max(peak, hi), min(trough, lo)
        if not exited:
            best, worst = max(best, hi), min(worst, lo)

        stop_hit = stop is not None and lo <= stop
        targ_hit = hi >= target

        # The +10% milestone, recorded the first time it is reached whether or
        # not it ends the signal. Credited only if no stop came first -- and
        # within one bar the stop is assumed first, see the docstring.
        if targ_hit and out["hit10_date"] is None and not out["hit_stop"]:
            if stop_hit:
                out["hit10_before_stop"] = False
            else:
                out.update(hit_target=True, hit10_before_stop=True,
                           hit10_date=ts.strftime("%Y-%m-%d"))

        if exited:
            continue
        if stop_hit:
            out.update(hit_stop=True, exit_price=round(float(stop), 4),
                       first_event_date=ts.strftime("%Y-%m-%d"))
            if out["hit10_date"] is None:
                out["hit10_before_stop"] = False
            exited = True
        elif targ_hit and CLOSE_ON_TARGET:
            out.update(exit_price=round(float(target), 4),
                       first_event_date=ts.strftime("%Y-%m-%d"))
            exited = True

    out["max_gain_pct"] = round((best / entry - 1) * 100, 2)
    out["max_dd_pct"] = round((worst / entry - 1) * 100, 2)
    out["peak_gain_pct"] = round((peak / entry - 1) * 100, 2)
    out["trough_dd_pct"] = round((trough / entry - 1) * 100, 2)
    return out


def decide_close(s: dict, walked: dict, age_days: int,
                 expire_days: int = EXPIRE_DAYS) -> dict | None:
    """Should this signal close, and why? None means leave it open.

    Order matters: a signal that hit its target and later faded closed at the
    target, not as a fade.

    hit_target_10 and stopped_out both assert which event came first, so both
    require walk_complete. An old signal whose early bars were outside the
    fetch window falls through to expired instead of being credited with a win
    or a loss the data cannot place in time.

    hit_target_10 only ever appears here with CLOSE_ON_TARGET set. By default
    reaching +10% is a milestone, not an ending -- see the constant.
    """
    if walked.get("walk_complete"):
        if CLOSE_ON_TARGET and walked.get("hit_target"):
            return {"close_reason": "hit_target_10",
                    "close_date": walked["first_event_date"],
                    "exit_price": walked["exit_price"]}
        if walked.get("hit_stop"):
            return {"close_reason": "stopped_out",
                    "close_date": walked["first_event_date"],
                    "exit_price": walked["exit_price"]}
    if age_days >= expire_days:
        return {"close_reason": "expired", "close_date": None,
                "exit_price": s.get("now_price")}
    # Only fade a signal once it has had a fair look; before that a dip is
    # noise, and the 30-day tier already shows it regardless of status.
    if age_days > 30 and s.get("status") == "FADING":
        return {"close_reason": "faded", "close_date": None,
                "exit_price": s.get("now_price")}
    return None
