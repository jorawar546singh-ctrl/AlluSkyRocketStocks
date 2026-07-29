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
import time

import pandas as pd
import requests

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
MAX_CANDIDATES = 150  # raised from 80 (2026-07-28): Finviz alone was already
# hitting 120 after the pagination fix, meaning the old 80 cap was actively
# discarding real candidates every day. Now paired with the relative-volume
# sort fix, so if this cap still binds, it drops the weakest movers, not an
# alphabetical or arbitrary slice.
NIFTY_500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
NIFTY_STATIC = os.path.join("data", "nifty500.csv")
TSX_WIKI_URL = "https://en.wikipedia.org/wiki/S%26P/TSX_Composite_Index"
TSX_STATIC = os.path.join("data", "tsx_composite.csv")

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
# Dropped ta_gap_u (2026-07-28): excluded quiet climbers that never gap;
# gappy names were also the likeliest to form wide/messy boxes (BLZE, CLSK).
FINVIZ_FILTER_BASE = "cap_smallover,sh_avgvol_o300,sh_relvol_o1.5,ta_perf_1w5o"
FINVIZ_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
    "Accept": "text/csv,text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finviz.com/screener.ashx",
}


def _finviz_csv(filt: str) -> list[str]:
    """Primary: Finviz CSV export endpoint. Clean columns, no HTML parsing."""
    url = f"https://finviz.com/export.ashx?v=111&f={filt}&ft=4&o=-relativevolume"
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


def _finviz_html(filt: str, max_pages: int = 12) -> list[str]:
    """Fallback: scrape the screener HTML. Finviz shows 20 rows per page
    (offset param r=1, r=21, r=41, ...) with nothing on the page itself
    indicating how many pages exist - so without pagination this silently
    caps at 20 tickers no matter how many actually match the filter.
    (Found 2026-07-28: this had been quietly capping the US universe at
    20 the whole time the HTML fallback was in use; it went unnoticed
    while the gap-up filter kept true match counts near/under 20 anyway.)
    Pages forward until a page returns no new tickers or comes back short
    (< 20 rows = last page), capped at max_pages (120 tickers) so a
    markup change can't cause a runaway loop against Finviz."""
    from bs4 import BeautifulSoup
    syms: list[str] = []
    for page in range(max_pages):
        offset = page * 20 + 1
        url = f"https://finviz.com/screener.ashx?v=111&f={filt}&ft=4&o=-relativevolume&r={offset}"
        r = requests.get(url, headers=FINVIZ_HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        page_syms: list[str] = []
        for a in soup.find_all("a", href=True):
            m = re.search(r"[?&]t=([A-Z][A-Z0-9.\-]{0,6})(?:&|$)", a["href"])
            if m and a.get_text(strip=True) == m.group(1):
                if m.group(1) not in page_syms:
                    page_syms.append(m.group(1))
        new = [s for s in page_syms if s not in syms]
        if not new:
            break
        syms.extend(new)
        if len(page_syms) < 20:
            break              # short page = last page, stop paging
        time.sleep(1)          # polite gap between pages
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


def ca_universe() -> list[str]:
    """S&P/TSX Composite constituents (~220 names), same design as India:
    no daily momentum pre-screen (Finviz doesn't meaningfully cover TSX) —
    the trend gate + Darvas trigger do ALL the filtering, same as they do
    against the full Nifty 500. Tries Wikipedia's maintained constituents
    table live first (dot-class tickers like TECK.B are converted to the
    dash form yfinance actually expects: TECK-B), falls back to the
    bundled CSV if the page structure changes or the fetch fails."""
    try:
        r = requests.get(TSX_WIKI_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        tables = pd.read_html(io.StringIO(r.text), keep_default_na=False)
        # the constituents table is the one with a "Ticker" column
        tbl = next((t for t in tables if "Ticker" in t.columns), None)
        if tbl is not None:
            syms = [str(s).strip().replace(".", "-")
                    for s in tbl["Ticker"].dropna().tolist()]
            syms = [s for s in syms if s and s.upper() != "NAN"]
            if len(syms) > 150:
                print(f"  universe: live TSX Composite (Wikipedia) ({len(syms)})")
                return syms
            _warn(f"Wikipedia TSX table returned only {len(syms)} rows")
        else:
            _warn("Wikipedia TSX page: no table with a 'Ticker' column found")
    except Exception as e:                                  # noqa: BLE001
        _warn(f"Wikipedia TSX fetch failed: {e}")

    if os.path.exists(TSX_STATIC):
        # keep_default_na=False matters here: National Bank of Canada's
        # real ticker is the literal string "NA", which pandas otherwise
        # silently reads as a null and drops — no error, just a quietly
        # wrong count. Found this while testing the fallback path.
        df = pd.read_csv(TSX_STATIC, keep_default_na=False)
        syms = [s.strip() for s in df["ticker"].tolist() if s.strip()]
        print(f"  universe: bundled CSV fallback ({len(syms)})")
        return syms

    _warn("No TSX universe available — scan aborted for CA")
    return []
