"""
Company name, sector, industry and next earnings date — per signal only.

Deliberately NOT per universe ticker. The universe runs to 400 names a day;
signals run to a handful. Looking these up for everything scanned would mean
400 extra requests per run for data that only matters once a breakout fires,
which is how you get rate-limited out of the thing that actually matters.

Sector and industry mostly arrive free from the universe page (Finviz's
Overview table, the NSE Industry column, the TSX Wikipedia table) — see
core/universe.SECTORS. This module fills the gaps and adds the two fields no
universe page carries: company name and the next earnings date.

Everything here fails soft to None. Company metadata is context, never a
reason to drop a signal or fail a run.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import requests

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
}
TIMEOUT = 15

# Finviz prints earnings as "Nov 04/a" or "Feb 12 AMC" depending on the view.
_EARN = re.compile(r"\b([A-Z][a-z]{2})\s+(\d{1,2})\b")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def _parse_earnings(text: str) -> str | None:
    """'Nov 04/a' -> '2026-11-04'.

    Finviz omits the year. The date is always the NEXT report, so a month
    earlier than today means next year -- without that rule a January date
    seen in December resolves eleven months into the past.
    """
    m = _EARN.search(text or "")
    if not m:
        return None
    mon = _MONTHS.get(m.group(1))
    if not mon:
        return None
    day = int(m.group(2))
    today = datetime.now(timezone.utc).date()
    year = today.year if (mon, day) >= (today.month, today.day) else today.year + 1
    try:
        return datetime(year, mon, day).strftime("%Y-%m-%d")
    except ValueError:          # e.g. Feb 30 from a bad parse
        return None


def lookup(ticker: str, market: str = "US") -> dict:
    """{company_name, sector, industry, next_earnings_date} — any may be None.

    Only Finviz is consulted, and only for US names: it does not meaningfully
    cover NSE or TSX, and guessing from a page that does not list the security
    is worse than leaving the field empty.
    """
    out = {"company_name": None, "sector": None,
           "industry": None, "next_earnings_date": None}
    if market != "US":
        return out
    try:
        r = requests.get(f"https://finviz.com/quote.ashx?t={ticker}",
                         headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return out
        html = r.text
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        # Sector / industry / country sit together as consecutive links on the
        # quote header. Matching on the href keeps this working when the
        # surrounding markup is restyled, which it is, often.
        crumbs = [a.get_text(strip=True) for a in soup.find_all("a", href=True)
                  if "screener.ashx?v=111&f=" in a["href"]
                  and ("sec_" in a["href"] or "ind_" in a["href"])]
        for a in soup.find_all("a", href=True):
            if "sec_" in a["href"] and not out["sector"]:
                out["sector"] = a.get_text(strip=True) or None
            elif "ind_" in a["href"] and not out["industry"]:
                out["industry"] = a.get_text(strip=True) or None

        # Company name is the page's only <h1>-ish title link to the quote.
        title = soup.find("title")
        if title:
            # "AAPL - Apple Inc. Stock Quote" -> "Apple Inc."
            t = title.get_text(strip=True)
            mt = re.match(rf"{re.escape(ticker)}\s*[-|]\s*(.+?)\s+(?:Stock|Quote|Share)", t, re.I)
            if mt:
                out["company_name"] = mt.group(1).strip() or None

        # The fundamentals table is label/value cell pairs.
        cells = [td.get_text(strip=True) for td in soup.find_all("td")]
        for i, c in enumerate(cells[:-1]):
            if c == "Earnings":
                out["next_earnings_date"] = _parse_earnings(cells[i + 1])
                break
    except Exception:                                       # noqa: BLE001
        pass
    return out


def days_to_earnings(date_str: str | None) -> int | None:
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        return (d - datetime.now(timezone.utc).date()).days
    except ValueError:
        return None
