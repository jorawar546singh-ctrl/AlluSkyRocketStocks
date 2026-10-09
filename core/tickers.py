"""
Pull stock tickers out of free-form chat text.

The hard part is not finding candidates, it is rejecting them. Trading chat is
full of capitalised words that look exactly like tickers -- "DD", "CEO", "EOD",
"ATH", "YOLO", "EPS" -- and a few that genuinely ARE both ("DD" is DuPont, "ALL"
is Allstate, "ON" is ON Semiconductor, "IT" is Gartner). A regex alone cannot
tell "I did my DD on this" from "bought DD calls".

So extraction runs in three passes, cheapest first:

  1. CASHTAGS ($AAPL)        accepted outright -- the $ is an explicit claim
  2. BARE CAPS (AAPL)        kept only if NOT in the stopword list below
  3. VALIDATION              survivors are priced; anything that does not
                             resolve to a real instrument is dropped

Pass 3 is what keeps the database clean, and it is why a false positive costs a
wasted price lookup rather than a fake signal. The stoplist exists to keep pass
3 cheap, not to be exhaustive.

Deliberately NOT handled: tickers written lowercase ("bought some aapl"). Case
is the only thing separating them from ordinary prose, and without it the false
positive rate is unusable.
"""
import re

# Chat words that are also valid tickers. Listing them here means they are only
# accepted with an explicit $ -- "my DD" is ignored, "$DD" is DuPont. The cost
# of this choice is missing a bare mention of a genuinely-traded word; the
# benefit is not logging every post that says "DD" as a stock pick.
AMBIGUOUS = {
    "DD", "ALL", "ON", "IT", "SO", "BY", "OR", "AT", "BE", "GO", "NOW", "OUT",
    "KEY", "CAR", "GAIN", "LOVE", "OPEN", "PLAY", "REAL", "RUN", "SEE", "TRUE",
    "WELL", "WOOF", "YOU", "FAST", "FUN", "HOPE", "LUV", "MOVE", "NICE", "PAY",
}

# Not tickers in any market: trading jargon, chat shorthand, common English.
STOPWORDS = {
    # trading jargon
    "ATH", "ATL", "EOD", "EOW", "YTD", "ROI", "EPS", "IPO", "ETF", "PE", "PT",
    "SL", "TP", "TA", "FA", "RSI", "MACD", "EMA", "SMA", "VWAP", "OTM", "ITM",
    "ATM", "IV", "DTE", "YOLO", "FOMO", "HODL", "BTD", "DCA", "AH", "PM",
    "PRE", "POST", "LONG", "SHORT", "CALL", "PUTS", "CALLS", "STOP", "LIMIT",
    "BULL", "BEAR", "GAP", "VOL", "AVG", "SUPP", "RES", "BREAK", "SWING",
    "SCALP", "RISK", "SIZE", "ENTRY", "EXIT", "TARGET", "LOSS", "PROFIT",
    # org / finance shorthand
    "CEO", "CFO", "CTO", "COO", "SEC", "FDA", "FED", "FOMC", "GDP", "CPI",
    "QE", "QT", "EU", "US", "USA", "UK", "CAD", "USD", "INR", "EUR", "GBP",
    "Q1", "Q2", "Q3", "Q4", "FY", "YOY", "QOQ", "MOM", "BPS", "AUM", "NAV",
    "NYSE", "AMEX", "TSX", "NSE", "BSE", "SPX", "NDX", "DJIA",
    # chat
    "LOL", "LMAO", "IMO", "IMHO", "IDK", "TBH", "FYI", "BTW", "AFAIK", "TLDR",
    "OMG", "WTF", "GG", "TY", "NP", "OK", "OP", "EDIT", "NEWS", "ALERT",
    "UPDATE", "WATCH", "LIST", "DAILY", "WEEKLY", "TODAY", "AM", "ET", "PT",
    "EST", "PST", "IST", "UTC", "MON", "TUE", "WED", "THU", "FRI",
    # generic caps that show up constantly
    "A", "I", "THE", "AND", "FOR", "BUT", "NOT", "YES", "NO", "NEW", "BIG",
    "HIGH", "LOW", "UP", "DOWN", "IN", "OF", "TO", "IS", "IF", "WAS", "ARE",
    "HAS", "HAD", "CAN", "WILL", "JUST", "ONLY", "ALSO", "MORE", "LESS",
    "GOOD", "BAD", "BEST", "WORST", "NEXT", "LAST", "FIRST", "ONE", "TWO",
}

# Length cap is 12, not the 5 a US-only reader would expect: NSE symbols run
# long (RELIANCE, TATAMOTORS, BAJAJFINSV) and would be silently truncated out
# of existence. '&' is in the class for names like M&M.
CASHTAG = re.compile(r"\$([A-Za-z][A-Za-z.\-&]{0,11})\b")
BARE = re.compile(r"\b([A-Z][A-Z.\-&]{0,11})\b")
# Markdown/URL noise that would otherwise yield fake candidates.
STRIP = re.compile(r"(```.*?```|`[^`]*`|https?://\S+|<[@#!&:][^>]*>)", re.S)


def extract(text: str) -> dict[str, str]:
    """Candidate tickers in `text`, mapped to how each was found.

    Returns {TICKER: 'cashtag' | 'bare'}. Nothing here is validated yet --
    callers run these through `validate()` before storing. A cashtag wins over
    a bare hit for the same symbol, since it is the stronger claim.
    """
    if not text:
        return {}
    clean = STRIP.sub(" ", text)
    found: dict[str, str] = {}

    for m in BARE.finditer(clean):
        sym = m.group(1).rstrip(".-")
        if len(sym) < 2 or sym in STOPWORDS or sym in AMBIGUOUS:
            continue
        if sym.isdigit():
            continue
        found[sym] = "bare"

    # Second so it overwrites a bare hit, and so $-prefixed ambiguous words
    # ("$DD", "$ALL") get through the stoplists that pass 2 applies.
    for m in CASHTAG.finditer(clean):
        sym = m.group(1).upper().rstrip(".-")
        if len(sym) < 1 or sym in STOPWORDS:
            continue
        found[sym] = "cashtag"

    return found


def validate(symbols, cfg, fetch_history) -> dict[str, float]:
    """Keep only symbols that resolve to a real instrument in this market.

    Returns {TICKER: last_close}. This is the pass that actually protects the
    database: a plausible-looking string that no exchange lists simply fails to
    price and is dropped. Suffixed per market (.NS, .TO) so an Indian mention
    is checked against the Indian listing, not a same-named US one.

    Fails soft -- a fetch outage drops the batch rather than inventing prices.
    """
    if not symbols:
        return {}
    suffixed = {s + cfg.ticker_suffix: s for s in symbols}
    try:
        hist = fetch_history(list(suffixed), period="1mo")
    except Exception as exc:                                   # noqa: BLE001
        print(f"  tickers: validation fetch failed ({exc}) — dropping batch")
        return {}

    out: dict[str, float] = {}
    for full, bare in suffixed.items():
        df = hist.get(full)
        if df is None or df.empty or "Close" not in df:
            continue
        closes = df["Close"].dropna()
        if closes.empty:
            continue
        price = float(closes.iloc[-1])
        if cfg.min_price <= price <= cfg.max_price:
            out[bare] = price
    return out
