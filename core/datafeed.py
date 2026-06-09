"""
Data feed: yfinance, batched, with exponential backoff.
Free tier rules: one bulk download per scan, never per-ticker loops for history.
"""
import time

import pandas as pd
import yfinance as yf

BATCH_SIZE = 40          # tickers per yf.download call
RETRIES = 3
BACKOFF_BASE = 8         # seconds: 8, 16, 32


def _download(tickers, period, interval="1d"):
    last_err = None
    for attempt in range(RETRIES):
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
        time.sleep(BACKOFF_BASE * (2 ** attempt))
    print(f"    datafeed: giving up on batch ({last_err})")
    return None


def fetch_history(tickers: list[str], period: str = "1y") -> dict[str, pd.DataFrame]:
    """
    Bulk-fetch daily OHLCV for many tickers.
    Returns {ticker: DataFrame} with only tickers that returned usable data.
    """
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        data = _download(batch, period)
        if data is None:
            continue
        if len(batch) == 1:
            df = data.dropna(how="all")
            if len(df):
                out[batch[0]] = df
        else:
            for t in batch:
                if t in data.columns.get_level_values(0):
                    df = data[t].dropna(how="all")
                    if len(df) > 30:
                        out[t] = df
        if i + BATCH_SIZE < len(tickers):
            time.sleep(2)  # polite gap between batches
    print(f"  datafeed: history for {len(out)}/{len(tickers)} tickers")
    return out
