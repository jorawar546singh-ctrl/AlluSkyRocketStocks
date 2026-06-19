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


# Finviz screener filter (shared by both CSV and HTML layers):
# small-cap+, avg vol >300k, price $1-100, rel-vol >1.5, gap up, 1w perf >5%
FINVIZ_FILTER = "cap_smallover,sh_avgvol_o300,sh_price_1to100,sh_relvol_o1.5,ta_gap_u,ta_perf_1w5o"
FINVIZ_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
    "Accept": "text/csv,text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finviz.com/screener.ashx",
}


def _finviz_csv() -> list[str]:
    """Primary: Finviz CSV export endpoint. Clean columns, no HTML parsing."""
    url = f"https://finviz.com/export.ashx?v=111&f={FINVIZ_FILTER}&ft=4"
    r = requests.get(url, headers=FINVIZ_HEADERS, timeout=20)
    if r.status_code != 200 or "," not in r.text[:200]:
        raise RuntimeError(f"CSV endpoint HTTP {r.status_code} / non-CSV body")
    df = pd.read_csv(io.StringIO(r.text))
    col = next((c for c in df.columns if c.strip().lower() == "ticker"), None)
    if not col:
        raise RuntimeError(f"no Ticker column; got {list(df.columns)[:5]}")
    syms = [str(s).strip().upper() for s in df[col].dropna()]
    if not syms:
        raise RuntimeError("CSV returned 0 rows")
    return syms


def _finviz_html() -> list[str]:
    """Fallback: scrape the screener HTML. Tries current + legacy selectors."""
    from bs4 import BeautifulSoup
    url = f"https://finviz.com/screener.ashx?v=111&f={FINVIZ_FILTER}&ft=4"
    r = requests.get(url, headers=FINVIZ_HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    syms: list[str] = []
    # current finviz markup: ticker links carry ?t=SYMBOL in the href
    for a in soup.find_all("a", href=True):
        m = re.search(r"[?&]t=([A-Z][A-Z0-9.\-]{0,6})(?:&|$)", a["href"])
        if m and a.get_text(strip=True) == m.group(1):
            if m.group(1) not in syms:
                syms.append(m.group(1))
    if not syms:
        raise RuntimeError("HTML parse found 0 tickers (markup changed)")
    return syms


def us_universe() -> list[str]:
    tickers: list[str] = []

    # Finviz: try CSV export, then HTML scrape. Either fills `tickers`.
    for layer, fn in (("CSV export", _finviz_csv), ("HTML scrape", _finviz_html)):
        try:
            tickers = fn()
            print(f"  universe: Finviz {layer} -> {len(tickers)} tickers")
            break
        except Exception as e:                              # noqa: BLE001
            # CSV needs Finviz Elite; failing through to HTML is expected,
            # so log it quietly. Only warn loudly if HTML (the real source) fails.
            if layer == "HTML scrape":
                _warn(f"Finviz HTML scrape failed: {e}")
            else:
                print(f"  universe: Finviz {layer} unavailable (free tier), trying HTML")

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
