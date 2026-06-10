"""
Universe builders. Ported from v1 (ttm_scanner / nifty_scanner), trimmed.
US : Finviz screener + Yahoo most-active/gainers (scrape, fail-soft)
IN : live NSE archives CSV -> bundled data/nifty500.csv fallback
Any layer that fails prints a loud warning so failures surface in the
Actions log and the Telegram digest instead of dying silently.
"""
import io
import os
import re

import pandas as pd
import requests

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
MAX_CANDIDATES = 80
NIFTY_500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
NIFTY_STATIC = os.path.join("data", "nifty500.csv")

WARNINGS: list[str] = []   # scanner.py appends these to the digest


def _warn(msg: str):
    print(f"  !! {msg}")
    WARNINGS.append(msg)


def us_universe() -> list[str]:
    from bs4 import BeautifulSoup
    tickers: list[str] = []

    url = ("https://finviz.com/screener.ashx?v=111&"
           "f=cap_smallover,sh_avgvol_o300,sh_price_1to100,sh_relvol_o1.5,"
           "ta_gap_u,ta_perf_1w5o&ft=4")
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for link in soup.find_all("a", class_="tab-link"):
            if link.get("href", "").startswith("quote.ashx?t="):
                t = link.text.strip()
                if t and t not in tickers:
                    tickers.append(t)
        if not tickers:
            _warn("Finviz returned 0 tickers — selector may have changed")
    except Exception as e:                                  # noqa: BLE001
        _warn(f"Finviz fetch failed: {e}")

    for label, url in (("most-active", "https://finance.yahoo.com/markets/stocks/most-active/"),
                       ("gainers", "https://finance.yahoo.com/markets/stocks/gainers/")):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                _warn(f"Yahoo {label} -> HTTP {r.status_code}")
                continue
            found = re.findall(r'href="/quote/([A-Z][A-Z0-9\-]{0,5})(?:[/?])', r.text)
            for sym in found:
                if sym.isalpha() and sym not in tickers:
                    tickers.append(sym)
        except Exception as e:                              # noqa: BLE001
            _warn(f"Yahoo {label} failed: {e}")

    return tickers[:MAX_CANDIDATES]


def in_universe() -> list[str]:
    try:
        r = requests.get(NIFTY_500_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        syms = [s.strip() for s in df["Symbol"].dropna().tolist() if not s.strip().upper().startswith("DUMMY")]
        if len(syms) > 400:
            print(f"  universe: live NSE archives ({len(syms)})")
            return syms
        _warn(f"NSE archives returned only {len(syms)} rows")
    except Exception as e:                                  # noqa: BLE001
        _warn(f"NSE archives fetch failed: {e}")

    if os.path.exists(NIFTY_STATIC):
        df = pd.read_csv(NIFTY_STATIC)
        syms = [s.strip() for s in df["Symbol"].dropna().tolist() if not s.strip().upper().startswith("DUMMY")]
        print(f"  universe: bundled CSV fallback ({len(syms)})")
        return syms

    _warn("No NIFTY universe available — scan aborted for IN")
    return []
