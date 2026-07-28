"""
Data feed: yfinance, batched, with exponential backoff.
Free tier rules: one bulk download per scan as the PRIMARY path — but if a
whole batch fails, fall back to retrying each ticker in it individually
rather than silently losing up to BATCH_SIZE tickers' worth of candidates
for the day. Every ticker still missing at the end is logged by name, not
just a count, so a bad day is diagnosable from the GitHub Actions log
instead of showing up only as "fewer signals than usual, no idea why."
"""
import time

import pandas as pd
import yfinance as yf

BATCH_SIZE = 40          # tickers per yf.download call
RETRIES = 3
BACKOFF_BASE = 8         # seconds: 8, 16, 32
SINGLE_RETRIES = 2       # smaller retry budget for the per-ticker fallback
SINGLE_BACKOFF = 5


def _download(tickers, period, interval="1d", retries=RETRIES, backoff=BACKOFF_BASE):
    last_err = None
    for attempt in range(retries):
        try:
            data = yf.download(
                tickers, period=period, interval=interval,
                group_by="ticker", progress=False, auto_adjust=False,
                threads=False,
            )
            if data is not None and len(data):
                return data
        except Exception as e:                      # noqa: BLE001
            last_err = e
        if attempt < retries - 1:
            time.sleep(backoff * (2 ** attempt))
    return None


def _extract(data, tickers):
    """Pull each ticker's DataFrame out of a (possibly multi-ticker) yfinance
    result. Returns (found: dict[ticker, df], missing: list[ticker])."""
    found, missing = {}, []
    if len(tickers) == 1:
        df = data
        # yfinance may return MultiIndex (ticker, field) columns even for a
        # single ticker when group_by="ticker" — unwrap it.
        if isinstance(df.columns, pd.MultiIndex):
            t = tickers[0]
            df = df[t] if t in df.columns.get_level_values(0) else df.droplevel(0, axis=1)
        df = df.dropna(how="all")
        if len(df):
            found[tickers[0]] = df
        else:
            missing.append(tickers[0])
        return found, missing
    for t in tickers:
        if t in data.columns.get_level_values(0):
            df = data[t].dropna(how="all")
            if len(df) > 30:
                found[t] = df
                continue
        missing.append(t)
    return found, missing


def fetch_history(tickers: list[str], period: str = "1y") -> dict[str, pd.DataFrame]:
    """
    Bulk-fetch daily OHLCV for many tickers.
    Returns {ticker: DataFrame} with only tickers that returned usable data.
    """
    out: dict[str, pd.DataFrame] = {}
    lost: list[str] = []

    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        data = _download(batch, period)

        if data is not None:
            found, missing = _extract(data, batch)
            out.update(found)
        else:
            missing = list(batch)   # whole batch failed outright

        if missing:
            # Fallback: retry the missing ones individually (smaller budget)
            # instead of writing off the whole batch. Often recovers most of
            # it — a batch usually fails from one bad symbol or a transient
            # rate limit, not every ticker in it being genuinely unavailable.
            for t in missing:
                d = _download([t], period, retries=SINGLE_RETRIES, backoff=SINGLE_BACKOFF)
                if d is not None:
                    found1, _ = _extract(d, [t])
                    if found1:
                        out.update(found1)
                        continue
                lost.append(t)

        if i + BATCH_SIZE < len(tickers):
            time.sleep(2)  # polite gap between batches

    print(f"  datafeed: history for {len(out)}/{len(tickers)} tickers")
    if lost:
        shown = ", ".join(lost[:20])
        more = f" (+{len(lost) - 20} more)" if len(lost) > 20 else ""
        print(f"  datafeed: {len(lost)} tickers lost after retries: {shown}{more}")
    return out
