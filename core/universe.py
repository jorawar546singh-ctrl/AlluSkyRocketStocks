"""
Universe builders. Ported from v1 (ttm_scanner / nifty_scanner), trimmed.
US : Yahoo screener (region, price, 3-month average volume) plus a bar
     post-filter for relative volume and 1-week performance. That pair
     approximates the original Finviz screen. Finviz is still queried and
     kept when it answers, and is never required.
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
MAX_CANDIDATES = 400  # raised from 150 (2026-10-10): the runs table showed the
# US universe coming back at exactly 150 on 18 of 19 runs -- the cap was
# binding EVERY day, so anything ranked 151st or lower by relative volume was
# invisible to the scanner. India already scans 500 tickers in ~107s and
# Canada 220 in ~49s, so 400 is well inside proven runtime.
# (Earlier history: 80 -> 150 on 2026-07-28, after the pagination fix showed
# Finviz alone was already returning 120.)
NIFTY_500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
NIFTY_STATIC = os.path.join("data", "nifty500.csv")
TSX_WIKI_URL = "https://en.wikipedia.org/wiki/S%26P/TSX_Composite_Index"
TSX_STATIC = os.path.join("data", "tsx_composite.csv")

WARNINGS: list[str] = []   # scanner.py appends these to the digest

# {bare ticker -> sector}, filled by whichever builder ran. Module-level for
# the same reason WARNINGS is: it keeps the builders' signatures alone.
# Sector is read from pages the scan ALREADY downloads (the Finviz Overview
# table, the NSE list's Industry column, the TSX Wikipedia table), so it costs
# no extra request. Every path fails soft to absent -- a missing sector must
# never cost a signal.
SECTORS: dict[str, str] = {}
NAMES: dict[str, str] = {}      # {bare ticker -> company name}, same free ride
INDUSTRIES: dict[str, str] = {}


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
# Minimum market cap for the US universe. 0 = no floor.
#
# Was $300M (Finviz's "cap_smallover"), which did not match the screener
# preset this scanner is meant to mirror -- that one sets Market Cap to Any.
# The gap was not academic: of 11 names the preset returned on 2026-10-19, SIX
# were below $300M and so could never be found however well everything else
# worked (BYRN $121M, ENGS $167M, EXYN $22M, SITC $187M, TCRT $9M, XLAB $103M).
#
# The $2 price floor and the 300K average-volume floor still exclude shells,
# which is what a cap floor was really guarding against. Watch the trend-gate
# pass rate after this: if the universe fills with microcaps that never clear
# the gate, the right answer is a smaller floor, not the old one.
US_MIN_MARKET_CAP = 0

# Original Finviz screen this scanner is meant to mirror:
#   sh_avgvol_o300   3-month average volume over 300k
#   sh_relvol_o1.5   relative volume over 1.5
#   ta_perf_1w5o     1-week performance over +5%
# plus a price floor near $2, appended from cfg in us_universe().
#
# Yahoo approximates the same four rules without Finviz. The EquityQuery
# applies US region, the price band, and avgdailyvol3m > 300k. Relative
# volume and the 1-week move are not screener fields in that form, so
# passes_yahoo_bar_filters() measures them on the daily bars the scan
# already downloads. Finviz stays an optional bonus when the export or
# HTML scrape actually returns rows.
FINVIZ_FILTER_BASE = "sh_avgvol_o300,sh_relvol_o1.5,ta_perf_1w5o"
FINVIZ_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
    "Accept": "text/csv,text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finviz.com/screener.ashx",
}


def _http_get(url: str, headers=None, timeout: int = 20):
    """GET with a browser's TLS fingerprint when one is available.

    Finviz answers the GitHub runner with a normal HTTP 200 whose body contains
    no ticker rows, while serving the same URL correctly to a browser. The
    usual cause is not headers but TLS fingerprinting: `requests` announces
    itself as Python during the handshake, before a single header is read, so
    no User-Agent string can disguise it.

    curl_cffi replays Chrome's actual TLS fingerprint and is already installed
    here as a yfinance dependency, so trying it first costs nothing. Falls back
    to plain requests if it is missing or errors.
    """
    try:
        from curl_cffi import requests as cr
        return cr.get(url, headers=headers, timeout=timeout, impersonate="chrome")
    except Exception as e:                                  # noqa: BLE001
        print(f"  universe: curl_cffi unavailable ({type(e).__name__}), using requests")
        return requests.get(url, headers=headers, timeout=timeout)


def _diagnose(r, where: str) -> str:
    """Say what actually came back, so a silent empty page is identifiable.

    Without this a bot-challenge page and a markup change look identical from
    the log: both are "0 tickers". They need opposite fixes.
    """
    try:
        body = r.text or ""
        title = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
        title = title.group(1).strip()[:80] if title else "(no title)"
        markers = [m for m in ("cloudflare", "captcha", "are you a robot",
                               "access denied", "unusual traffic", "cf-chl",
                               "just a moment", "enable javascript")
                   if m in body.lower()]
        note = (f"{where}: HTTP {r.status_code}, {len(body)} bytes, "
                f"title={title!r}")
        if markers:
            note += f", BOT-CHALLENGE markers: {markers}"
        elif len(body) > 20000:
            note += ", large body but no rows parsed -> markup likely changed"
        else:
            note += ", small body -> blocked or empty result"
        print(f"  universe: {note}")
        return note
    except Exception:                                       # noqa: BLE001
        return f"{where}: could not inspect response"


def _finviz_csv(filt: str) -> list[str]:
    """Primary: Finviz CSV export endpoint. Clean columns, no HTML parsing."""
    url = f"https://finviz.com/export.ashx?v=111&f={filt}&ft=4&o=-relativevolume"
    r = _http_get(url, headers=FINVIZ_HEADERS, timeout=20)
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


def _finviz_row(row) -> str | None:
    """Pull ticker, company, sector and industry out of one screener row.

    Finviz rebuilt the screener as a div grid: there is no <table>, no <tr>
    and no <td> on the page any more, and ticker links are /quote?t=SYM rather
    than quote.ashx?t=SYM. The old parser matched none of that and returned
    zero tickers from a perfectly good 422KB page, which is what collapsed the
    US universe -- it was never an IP block.

    The replacement is sturdier than what it replaces. Each row carries
    data-row-key="SYM" and each cell a data-column-id (row, ticker, company,
    sector, industry, country, marketCap, PE, price, change, volume), so the
    fields are read by NAME instead of by counting columns. A reordered or
    inserted column no longer silently shifts sector into industry.

    Note the ticker CELL's text is doubled ("WWORX") by a logo glyph, so the
    symbol comes from data-row-key, never from that cell.
    """
    try:
        sym = (row.get("data-row-key") or "").strip().upper()
        if not sym:
            return None
        cells = {}
        for c in row.select("[data-column-id]"):
            cid = c.get("data-column-id")
            if cid and cid not in cells:
                cells[cid] = c.get_text(strip=True)

        name = cells.get("company")
        if name and 1 < len(name) <= 80:
            NAMES[sym] = name
        sector = cells.get("sector")
        # A real sector is a short phrase, not a number or a half-line of text.
        if sector and 2 < len(sector) <= 40 and not any(ch.isdigit() for ch in sector):
            SECTORS[sym] = sector
        industry = cells.get("industry")
        if industry and 2 < len(industry) <= 60:
            INDUSTRIES[sym] = industry
        return sym
    except Exception:                                       # noqa: BLE001
        return None


def _finviz_html(filt: str, max_pages: int = 25) -> list[str]:   # 25 x 20 = 500 rows,
    # enough to fill MAX_CANDIDATES=400. Raising the cap without raising this
    # would have changed nothing: 12 pages tops out at 240.
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
    last_resp = None
    for page in range(max_pages):
        offset = page * 20 + 1
        # The canonical path is /screener/stocks now; screener.ashx still
        # redirects there, and the r= offset works on both.
        url = f"https://finviz.com/screener/stocks?v=111&f={filt}&ft=4&o=-relativevolume&r={offset}"
        r = _http_get(url, headers=FINVIZ_HEADERS, timeout=20)
        r.raise_for_status()
        last_resp = r
        soup = BeautifulSoup(r.text, "html.parser")
        page_syms: list[str] = []
        for row in soup.select("[data-row-key]"):
            sym = _finviz_row(row)
            if sym and sym not in page_syms:
                page_syms.append(sym)
        new = [s for s in page_syms if s not in syms]
        if not new:
            break
        syms.extend(new)
        if len(page_syms) < 20:
            break              # short page = last page, stop paging
        time.sleep(1)          # polite gap between pages
    if not syms:
        # "0 tickers" alone cannot distinguish a bot-challenge page from a
        # markup change, and those need opposite fixes. Say which.
        detail = _diagnose(last_resp, "finviz screener") if last_resp is not None \
            else "no response captured"
        raise RuntimeError(f"HTML parse found 0 tickers — {detail}")
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


# Yahoo's predefined screeners, as the primary US source.
#
# Finviz blocks datacenter IPs on screener.ashx, so from GitHub Actions it
# returns nothing while working perfectly from a home browser -- that is what
# dropped the universe from 380 names to 48 on 2026-10-19. Yahoo is demonstrably
# reachable from the runner: every price bar in this project comes from it, and
# the 48 survivors came from its HTML pages. So the universe comes from Yahoo
# too, and Finviz becomes a bonus rather than the dependency.
#
# These screens are chosen to overlap the Finviz filter's intent -- movers on
# unusual volume -- rather than to be a broad market list. The response carries
# price and average volume per name, so those two are applied on the quotes.
# Relative volume and 1-week performance are applied later, on daily bars
# (passes_yahoo_bar_filters): Yahoo's screener has no operand that matches
# Finviz's sh_relvol_o1.5 or ta_perf_1w5o.
YAHOO_SCREENS = (
    "day_gainers",
    "most_actives",
    "small_cap_gainers",
    "aggressive_small_caps",
    "growth_technology_stocks",
    "undervalued_growth_stocks",
)
YAHOO_MIN_AVG_VOL = 300_000     # Finviz sh_avgvol_o300 (the "300" is thousands)
YAHOO_MIN_REL_VOL = 1.5         # Finviz sh_relvol_o1.5
YAHOO_MIN_WEEK_PERF = 0.05      # Finviz ta_perf_1w5o: strictly over +5%
# Prior sessions used as the relative-volume baseline, today excluded so the
# session being judged cannot lift its own denominator. Finviz divides by the
# 3-month average; ~20 sessions is the same window the Darvas trigger already
# uses for vol_ratio. A shorter history fails the check.
YAHOO_REL_VOL_SESSIONS = 20


def _quotes_of(res) -> list[dict]:
    """yf.screen returns the raw Yahoo body; be liberal about its shape."""
    if isinstance(res, dict):
        if "quotes" in res:
            return res.get("quotes") or []
        inner = (res.get("finance", {}) or {}).get("result") or []
        if inner:
            return inner[0].get("quotes") or []
    return []


def _keep(cfg, quotes, seen: set) -> list[str]:
    """Apply the numeric filters locally, on the numbers Yahoo returned."""
    out = []
    for q in quotes:
        sym = (q.get("symbol") or "").strip().upper()
        # US common stock only: dotted/dashed class tickers (BRK-B) and
        # suffixed foreign listings are different instruments.
        if not sym or not sym.isalpha() or len(sym) > 5 or sym in seen:
            continue
        price = q.get("regularMarketPrice")
        avgvol = (q.get("averageDailyVolume3Month")
                  or q.get("averageDailyVolume10Day"))
        if price is not None and not (cfg.min_price <= price <= cfg.max_price):
            continue
        if avgvol is not None and avgvol < YAHOO_MIN_AVG_VOL:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def passes_yahoo_bar_filters(df: pd.DataFrame) -> bool:
    """Relative volume and 1-week performance, measured on daily bars.

    Yahoo approximates Finviz here. The screener query already required US
    region, the price band, and 3-month average volume over 300k. These two
    clauses of FINVIZ_FILTER_BASE have no matching screener operand, so they
    are checked on the OHLCV the scan downloads for every candidate:

      relative volume     last volume / mean(prior ~20 sessions) >= 1.5
      1-week performance  close[-1] / close[-6] - 1 > +5%

    Today is left out of the volume baseline. Fail closed when there are not
    enough bars to measure either one: a missing bar is not a pass.
    """
    if df is None or df.empty:
        return False
    if "Close" not in df.columns or "Volume" not in df.columns:
        return False
    d = df.dropna(subset=["Close", "Volume"])
    need = max(YAHOO_REL_VOL_SESSIONS + 1, 6)
    if len(d) < need:
        return False
    vol_today = float(d["Volume"].iloc[-1])
    vol_avg = float(d["Volume"].iloc[-(YAHOO_REL_VOL_SESSIONS + 1):-1].mean())
    if not (vol_avg > 0) or vol_today / vol_avg < YAHOO_MIN_REL_VOL:
        return False
    prev_close = float(d["Close"].iloc[-6])
    last_close = float(d["Close"].iloc[-1])
    if not (prev_close > 0):
        return False
    # close[-1] / close[-6] - 1, written as a difference so an exact +5%
    # (10.50 vs 10.00) does not pass on floating-point dust.
    return (last_close - prev_close) / prev_close > YAHOO_MIN_WEEK_PERF


def filter_us_histories(histories: dict, suffix: str = "",
                        exempt: set | None = None) -> tuple[dict, int]:
    """Drop US frames that miss the Finviz relative-volume / 1-week rules.

    `exempt` is bare tickers that skip the check (mentions: a tip is in the
    universe because someone named it, not because a screener returned it).
    India and Canada do not call this. Returns (kept, dropped_count).
    """
    exempt = {(s or "").strip().upper() for s in (exempt or set())}
    kept: dict = {}
    dropped = 0
    for ticker, df in histories.items():
        bare = ticker.removesuffix(suffix) if suffix else ticker
        if bare.upper() in exempt or passes_yahoo_bar_filters(df):
            kept[ticker] = df
        else:
            dropped += 1
    return kept, dropped


def _yahoo_screener(cfg, target: int = 400) -> list[str]:
    """Tickers from Yahoo's screener, via yfinance.

    Goes through yfinance rather than plain requests because Yahoo's screener
    endpoint requires an auth cookie and crumb: a bare GET returns HTTP 429 from
    a datacenter IP, which is exactly what it did on the first run. yfinance
    already negotiates and caches that handshake for every price download in
    this project, so reusing it costs nothing and removes a second thing to
    keep working.

    A custom query first: US region, the configured price band, and 3-month
    average volume over 300k (Finviz's sh_avgvol_o300). Sorted by percent
    change so the day's movers come first -- that is where breakouts live,
    and it matters because the list is capped. Relative volume and the
    1-week move are applied afterwards on daily bars, not in this query.
    Then the predefined screens top it up with names the custom query
    ranked too low.
    """
    import yfinance as yf

    out: list[str] = []
    seen: set[str] = set()

    try:
        from yfinance import EquityQuery as EQ
        clauses = [
            EQ("eq", ["region", "us"]),
            EQ("gt", ["intradayprice", float(cfg.min_price)]),
            EQ("lt", ["intradayprice", float(cfg.max_price)]),
            # 3-month average volume, not today's print. dayvolume let a
            # one-off session pass the average-volume rule.
            EQ("gt", ["avgdailyvol3m", YAHOO_MIN_AVG_VOL]),
        ]
        # Only when a floor is actually set -- see US_MIN_MARKET_CAP.
        if US_MIN_MARKET_CAP > 0:
            clauses.append(EQ("gt", ["intradaymarketcap", US_MIN_MARKET_CAP]))
        q = EQ("and", clauses)
        for offset in range(0, target, 250):
            res = yf.screen(q, offset=offset, size=250,
                            sortField="percentchange", sortAsc=False)
            quotes = _quotes_of(res)
            new = _keep(cfg, quotes, seen)
            out.extend(new)
            print(f"  universe: Yahoo custom screen +{len(new)} (of {len(quotes)}) "
                  f"at offset {offset}")
            if len(quotes) < 250 or len(out) >= target:
                break
    except Exception as e:                                  # noqa: BLE001
        _warn(f"Yahoo custom screen failed: {e}")

    # Predefined screens, for names the custom query ranked below the cap.
    if len(out) < target:
        for scr in YAHOO_SCREENS:
            if len(out) >= target:
                break
            try:
                new = _keep(cfg, _quotes_of(yf.screen(scr, count=250)), seen)
                out.extend(new)
                print(f"  universe: Yahoo {scr} -> +{len(new)}")
            except Exception as e:                          # noqa: BLE001
                _warn(f"Yahoo screener {scr} failed: {e}")
    return out[:target]


MENTION_DAYS = 14   # a tip older than this is not a fresh idea any more


def mentioned_recently(market_key: str, days: int = MENTION_DAYS) -> list[str]:
    """Tickers someone flagged lately (Discord, manual list).

    These are ADDED to the universe, not substituted for it. They skip the US
    relative-volume / 1-week bar filter, which is the screener's admission
    rule — a tip exists so a name the screener missed still gets looked at.
    Once in, a tip earns a signal by passing the same trend gate, Darvas
    trigger and price bounds as everything else.

    Imported lazily so a db without the mentions table (or a fresh checkout)
    cannot break the scan.
    """
    try:
        from core.db import connect
        with connect() as con:
            rows = con.execute(
                "SELECT DISTINCT ticker FROM mentions WHERE market=? "
                "AND julianday('now') - julianday(mention_date) <= ?",
                (market_key, days)).fetchall()
        return [r["ticker"] for r in rows]
    except Exception as exc:                                   # noqa: BLE001
        _warn(f"could not read mentions ({exc})")
        return []


def us_universe(cfg) -> list[str]:
    tickers: list[str] = []
    pf = _price_filter(cfg)
    filt = f"{FINVIZ_FILTER_BASE},{pf}"
    print(f"  universe: price filter -> {pf}  (from cfg: ${cfg.min_price}-${cfg.max_price})")

    # Yahoo first. It is the source that actually answers from a datacenter IP.
    # The query enforces region, price and 3-month average volume. Relative
    # volume and the 1-week move are applied in the scanner once daily bars
    # exist (filter_us_histories); they are not screener fields.
    tickers = _yahoo_screener(cfg)
    print(f"  universe: Yahoo screeners -> {len(tickers)} tickers")

    # Finviz second, as a bonus. It adds names Yahoo's screens miss, and its
    # rows also populate SECTORS/NAMES/INDUSTRIES for free. It returns nothing
    # from GitHub Actions (datacenter IPs are blocked on screener.ashx) and
    # everything from a home connection, so it must never be load-bearing.
    for layer, fn in (("CSV export", _finviz_csv), ("HTML scrape", _finviz_html)):
        try:
            extra = fn(filt)
            new = [t for t in extra if t not in set(tickers)]
            tickers = list(tickers) + new
            print(f"  universe: Finviz {layer} -> +{len(new)} tickers")
            break
        except Exception as e:                              # noqa: BLE001
            # Expected on the runner. Not a warning unless Yahoo also came up
            # short, because a working scan does not need Finviz at all.
            msg = f"Finviz {layer} unavailable: {e}"
            if layer == "HTML scrape":
                if len(tickers) < 100:
                    _warn(msg + " — and Yahoo returned too few to cover for it")
                else:
                    print(f"  universe: {msg} (Yahoo covered it)")
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


def _sectors_from_frame(df, sym_col: str, sector_col: str) -> None:
    """Fill SECTORS from a dataframe that already has a sector-ish column.

    Used for the NSE list ("Industry") and the TSX Wikipedia table
    ("Sector"). Silent when the column is absent -- these are third-party
    files whose headers change without notice, and a sector is never worth
    failing a scan over.
    """
    try:
        if sector_col not in df.columns or sym_col not in df.columns:
            return
        for sym, sec in zip(df[sym_col], df[sector_col]):
            sym = str(sym).strip()
            sec = str(sec).strip()
            if sym and sec and sec.lower() not in ("nan", "", "-"):
                SECTORS[sym] = sec
    except Exception:                                       # noqa: BLE001
        pass


def in_universe() -> list[str]:
    try:
        r = requests.get(NIFTY_500_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        syms = [s.strip() for s in df["Symbol"].dropna().tolist() if not s.strip().upper().startswith("DUMMY")]
        _sectors_from_frame(df, "Symbol", "Industry")
        if len(syms) > 400:
            print(f"  universe: live NSE archives ({len(syms)})")
            return syms
        _warn(f"NSE archives returned only {len(syms)} rows")
    except Exception as e:                                  # noqa: BLE001
        _warn(f"NSE archives fetch failed: {e}")

    if os.path.exists(NIFTY_STATIC):
        df = pd.read_csv(NIFTY_STATIC)
        syms = [s.strip() for s in df["Symbol"].dropna().tolist() if not s.strip().upper().startswith("DUMMY")]
        _sectors_from_frame(df, "Symbol", "Industry")
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
            # Wikipedia has titled this column "Sector" and "GICS Sector" at
            # different times; try both, and carry the same dot->dash fix the
            # tickers get so the keys match.
            # Wikipedia has titled this column several ways over the years
            # ("Sector", "GICS Sector", "Industry"), so match on content
            # rather than an exact name -- the fixed list was returning
            # nothing, which is why every CA signal had a null sector.
            col = next((c for c in tbl.columns
                        if "sector" in str(c).lower() or "industry" in str(c).lower()),
                       None)
            if col:
                t2 = tbl.copy()
                t2["Ticker"] = t2["Ticker"].astype(str).str.strip().str.replace(".", "-", regex=False)
                _sectors_from_frame(t2, "Ticker", col)
            else:
                _warn(f"TSX table has no sector-like column; got {list(tbl.columns)[:6]}")
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
