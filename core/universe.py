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
# small-cap+, avg vol >300k, rel-vol >1.5, gap up, 1w perf >5%.
# Price lower-bound is appended dynamically from cfg.min_price in us_universe() —
# do NOT hardcode a price bucket here, or it'll silently drift from core/config.py
# (this happened once already: a stale "sh_price_1to100" kept the universe capped
# at $100 even after max_price was raised to $1000).
FINVIZ_FILTER_BASE = "cap_smallover,sh_avgvol_o300,sh_relvol_o1.5,ta_gap_u,ta_perf_1w5o"
FINVIZ_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
    "Accept": "text/csv,text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finviz.com/screener.ashx",
}


def _finviz_csv(filt: str) -> list[str]:
    """Primary: Finviz CSV export endpoint. Clean columns, no HTML parsing."""
    url = f"https://finviz.com/export.ashx?v=111&f={filt}&ft=4"
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


def _finviz_html(filt: str) -> list[str]:
    """Fallback: scrape the screener HTML. Tries current + legacy selectors."""
    from bs4 import BeautifulSoup
    url = f"https://finviz.com/screener.ashx?v=111&f={filt}&ft=4"
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


# Finviz's price filter only ships fixed presets, not arbitrary numbers.
_VALID_PRICE_PRESETS = (1, 2, 3, 4, 5, 7, 10, 15, 20, 30, 40, 50, 60, 70, 80, 90, 100)


def _price_filter(cfg) -> str:
    """Build the Finviz price clause FROM cfg.min_price/max_price, so the
    universe-source filter can never silently drift from core/config.py
    again. Lower bound snaps to the nearest valid preset at/below min_price.
    Upper bound is only added if max_price <= 100 (Finviz's highest preset);
    above that there's no matching preset, so we skip it here and let
    scanner.py's `cfg.min_price <= price <= cfg.max_price` check be the
    real enforcement for the upper end — Finviz is just a coarse pre-filter."""
    lo = max((p for p in _VALID_PRICE_PRESETS if p <= cfg.min_price), default=1)
    parts = [f"sh_price_o{lo}"]
    if cfg.max_price <= 100:
        hi = min((p for p in _VALID_PRICE_PRESETS if p >= cfg.max_price), default=100)
        parts.append(f"sh_price_u{hi}")
    return ",".join(parts)


def us_universe(cfg) -> list[str]:
    tickers: list[str] = []
    pf = _price_filter(cfg)
    filt = f"{FINVIZ_FILTER_BASE},{pf}"
    print(f"  universe: price filter -> {pf}  (from cfg: ${cfg.min_price}-${cfg.max_price})")

    # Finviz: try CSV export, then HTML scrape. Either fills `tickers`.
    for layer, fn in (("CSV export", _finviz_csv), ("HTML scrape", _finviz_html)):
        try:
            tickers = fn(filt)
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
