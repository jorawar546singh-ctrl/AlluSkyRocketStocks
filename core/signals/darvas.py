"""
Darvas trigger: 14-day box top broken on >= 2x average volume.
Same core logic as v1 ttm_scanner / nifty_scanner, reduced to a pure function:
takes a price DataFrame, returns metrics or None. No I/O, no side effects.
"""
import pandas as pd


def evaluate(df: pd.DataFrame, box_days: int = 14, vol_mult: float = 2.0) -> dict | None:
    df = df.dropna(subset=["Close", "High", "Low", "Volume"])
    if len(df) < box_days + 21:
        return None

    today = df.iloc[-1]
    price = float(today["Close"])
    vol_today = float(today["Volume"])

    # Box = the N sessions *before* today
    box = df.iloc[-(box_days + 1):-1]
    box_top = float(box["High"].max())
    box_bottom = float(box["Low"].min())
    if box_top <= 0:
        return None

    vol_avg = float(df["Volume"].iloc[-21:-1].mean())
    if vol_avg <= 0:
        return None
    vol_ratio = vol_today / vol_avg

    broke_out = price > box_top
    volume_ok = vol_ratio >= vol_mult
    if not (broke_out and volume_ok):
        return None

    suggested_stop = round(box_top * 0.98, 2)        # just under the box top
    risk_pct = round((price - suggested_stop) / price * 100, 2)
    if risk_pct <= 0:
        return None
    target = round(price * 1.30, 2)
    rr_ratio = round((target - price) / (price - suggested_stop), 2)

    return {
        "price": round(price, 2),
        "box_top": round(box_top, 2),
        "box_bottom": round(box_bottom, 2),
        "pct_above_box": round((price - box_top) / box_top * 100, 2),
        "vol_ratio": round(vol_ratio, 2),
        "suggested_stop": suggested_stop,
        "risk_pct": risk_pct,
        "rr_ratio": rr_ratio,
    }
