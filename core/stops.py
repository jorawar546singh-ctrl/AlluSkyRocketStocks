"""
Stop calculators — two different jobs, both box/profit aware.

position_trailing_stop(): for things you OWN. Ratchets up, never down.
    +10% gain -> stop to breakeven (entry)
    +20% gain -> stop trails 10% below the running high
    +30% gain -> stop trails 7%  below the running high
    The returned stop is never lower than the position's current_stop.

watchlist_entry_stop(): for things you DON'T own yet but might buy TODAY.
    Recomputes the Darvas box AS OF the latest bar and returns a stop just
    under the current box bottom — i.e. the stop for entering at today's
    price, not the stale flag-time stop.

Both are advisory. The broker order is what actually protects the trade.
"""
import pandas as pd


def position_trailing_stop(entry: float, current_stop: float, running_high: float,
                           now_price: float) -> tuple[float, float]:
    """Return (new_stop, new_running_high). Stop only ratchets up."""
    high = max(running_high or entry, now_price)
    gain = (now_price - entry) / entry * 100

    if gain >= 30:
        candidate = high * 0.93          # trail 7% below high
    elif gain >= 20:
        candidate = high * 0.90          # trail 10% below high
    elif gain >= 10:
        candidate = entry                # lock breakeven
    else:
        candidate = current_stop         # untouched

    new_stop = max(candidate, current_stop or 0)
    return round(new_stop, 2), round(high, 2)


def watchlist_entry_stop(df: pd.DataFrame, box_days: int = 14) -> float | None:
    """Stop just under the CURRENT 14-day Darvas box bottom (for entering today)."""
    box = df.dropna(subset=["Low"]).iloc[-(box_days + 1):-1]
    if len(box) < box_days // 2:
        return None
    box_bottom = float(box["Low"].min())
    return round(box_bottom * 0.98, 2)
