"""
TTM Breakout Scanner  --  cloud edition (widened universe)
-----------------------------------------------------------
Finds Darvas-style breakouts across the mid-to-small cap universe
($2-$100 price range), with 5-factor scoring and A+/A/B/C/D grading.

Sources (combined, de-duplicated):
  - Finviz screener: cap_midunder (mid, small, micro), price $2-$100,
    relvol > 1.5, gap up, 1-week perf up
  - Yahoo Finance: most-active + gainers pages

Verification:
  - Yahoo Finance daily OHLCV via yfinance

Run:  python3 ttm_scanner.py
"""

import time
import sys
import os
import json
import re
from datetime import datetime

import requests
import pandas as pd
import yfinance as yf
from bs4 import BeautifulSoup


# ============================================================
# SECRETS FROM ENVIRONMENT
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
SEND_TELEGRAM = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


# ============================================================
# SCANNER SETTINGS
# ============================================================
DARVAS_BOX_DAYS = 14
VOLUME_MULTIPLIER = 2.0
MIN_PRICE = 2.0
MAX_PRICE = 100.0      # widened from 30 -> 100 to include mid-caps
MAX_RESULTS_TO_CHECK = 80
TOP_N_TO_SHOW = 12

DEDUP_HOURS = 6
DEDUP_FILE = "alerted.log"
LATEST_JSON = "latest.json"
HISTORY_JSON = "history.json"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def script_path(name):
    return os.path.join(SCRIPT_DIR, name)


# ============================================================
# SOURCE 1: Finviz (widened to mid-caps, $2-$100)
# ============================================================
def get_finviz_candidates():
    # cap_midunder = small + mid + micro. sh_price_o1u100 = $1-$100 range.
    url = (
        "https://finviz.com/screener.ashx?"
        "v=111&"
        "f=cap_midunder,sh_avgvol_o300,sh_price_2to100,sh_relvol_o1.5,ta_gap_u,ta_perf_1w10o"
        "&ft=4"
    )
    print("  [Finviz] fetching...")
    try:
        response = requests.get(url, headers=DEFAULT_HEADERS, timeout=15)
        response.raise_for_status()
    except Exception as e:
        print(f"    Finviz failed: {e}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    tickers = []
    for link in soup.find_all("a", class_="tab-link"):
        href = link.get("href", "")
        if href.startswith("quote.ashx?t="):
            ticker = link.text.strip()
            if ticker and ticker not in tickers:
                tickers.append(ticker)
    print(f"    Finviz returned {len(tickers)} candidates.")
    return tickers


# ============================================================
# SOURCE 2: Yahoo most-active + gainers
# ============================================================
def get_yahoo_candidates():
    print("  [Yahoo] fetching most-active + gainers...")
    pages = [
        ("most-active", "https://finance.yahoo.com/markets/stocks/most-active/"),
        ("gainers",     "https://finance.yahoo.com/markets/stocks/gainers/"),
    ]
    tickers = []
    for label, url in pages:
        try:
            r = requests.get(url, headers=DEFAULT_HEADERS, timeout=15)
            if r.status_code != 200:
                print(f"    Yahoo {label} -> status {r.status_code}")
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            found = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                m = re.match(r"^/quote/([A-Z][A-Z0-9\-]{0,5})(?:[/?]|$)", href)
                if m:
                    sym = m.group(1).upper()
                    if sym.isalpha() and len(sym) <= 5 and sym not in found:
                        found.append(sym)
            for sym in found:
                if sym not in tickers:
                    tickers.append(sym)
            print(f"    Yahoo {label} returned {len(found)} tickers.")
        except Exception as e:
            print(f"    Yahoo {label} error: {e}")
            continue
    print(f"    Yahoo total unique: {len(tickers)}")
    return tickers


def get_all_candidates():
    print("Gathering candidates from all sources...")
    finviz = get_finviz_candidates()
    yahoo  = get_yahoo_candidates()
    seen = set()
    combined = []
    for t in finviz + yahoo:
        if t not in seen:
            seen.add(t)
            combined.append(t)
    print(f"  Combined (de-duped): {len(combined)} tickers "
          f"(Finviz {len(finviz)} + Yahoo {len(yahoo)})\n")
    return combined[:MAX_RESULTS_TO_CHECK]


# ============================================================
# Darvas verification + scoring
# ============================================================
def check_darvas_breakout(ticker):
    try:
        data = yf.download(
            ticker, period="60d", interval="1d",
            progress=False, auto_adjust=False,
        )
    except Exception:
        return None

    if data is None or len(data) < DARVAS_BOX_DAYS + 5:
        return None

    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    today = data.iloc[-1]
    today_close = float(today["Close"])
    today_volume = float(today["Volume"])

    if not (MIN_PRICE <= today_close <= MAX_PRICE):
        return None

    prior = data.iloc[-(DARVAS_BOX_DAYS + 1):-1]
    box_top = float(prior["High"].max())
    box_bottom = float(prior["Low"].min())

    if today_close <= box_top:
        return None

    vol_lookback = min(20, len(data) - 1)
    avg_vol = float(data["Volume"].iloc[-(vol_lookback + 1):-1].mean())
    if avg_vol <= 0 or today_volume < avg_vol * VOLUME_MULTIPLIER:
        return None

    vol_ratio = today_volume / avg_vol
    pct_above_box = (today_close - box_top) / box_top * 100

    closes = data["Close"].astype(float)
    ma20 = float(closes.iloc[-20:].mean())
    ma20_prev = float(closes.iloc[-21:-1].mean())
    ma_rising = ma20 > ma20_prev
    box_range_pct = (box_top - box_bottom) / box_top * 100

    stop = box_top * 0.98
    target = today_close * 1.30
    risk = today_close - stop
    reward = target - today_close
    rr_ratio = reward / risk if risk > 0 else 0
    risk_pct = risk / today_close * 100

    spark = [round(float(x), 2) for x in closes.iloc[-30:].tolist()]

    hit = {
        "ticker": ticker,
        "price": round(today_close, 2),
        "box_top": round(box_top, 2),
        "box_bottom": round(box_bottom, 2),
        "pct_above_box": round(pct_above_box, 2),
        "vol_today": int(today_volume),
        "vol_avg_20d": int(avg_vol),
        "vol_ratio": round(vol_ratio, 2),
        "suggested_stop": round(stop, 2),
        "target_30pct": round(target, 2),
        "risk_pct": round(risk_pct, 2),
        "rr_ratio": round(rr_ratio, 2),
        "box_range_pct": round(box_range_pct, 2),
        "ma20": round(ma20, 2),
        "ma_rising": ma_rising,
        "price_vs_ma20_pct": round((today_close - ma20) / ma20 * 100, 2),
        "spark": spark,
    }
    hit.update(score_breakout(hit))
    return hit


def score_breakout(h):
    v = h["vol_ratio"]
    if v >= 5.0:   vol_score = 20
    elif v >= 4.0: vol_score = 18
    elif v >= 3.0: vol_score = 14
    elif v >= 2.5: vol_score = 11
    elif v >= 2.0: vol_score = 8
    else:          vol_score = 4

    p = h["pct_above_box"]
    if p >= 6:      clarity_score = 20
    elif p >= 4:    clarity_score = 16
    elif p >= 2.5:  clarity_score = 13
    elif p >= 1.5:  clarity_score = 10
    elif p >= 0.5:  clarity_score = 6
    else:           clarity_score = 3

    r = h["box_range_pct"]
    if r <= 10:    box_score = 20
    elif r <= 15:  box_score = 16
    elif r <= 20:  box_score = 12
    elif r <= 25:  box_score = 8
    elif r <= 35:  box_score = 5
    else:          box_score = 2

    rr = h["rr_ratio"]
    if rr >= 5:     rr_score = 20
    elif rr >= 4:   rr_score = 16
    elif rr >= 3:   rr_score = 12
    elif rr >= 2:   rr_score = 8
    elif rr >= 1.5: rr_score = 5
    else:           rr_score = 2

    pct_ma = h["price_vs_ma20_pct"]
    rising = h["ma_rising"]
    if rising and pct_ma >= 10:  trend_score = 20
    elif rising and pct_ma >= 5: trend_score = 16
    elif rising and pct_ma >= 0: trend_score = 12
    elif rising:                 trend_score = 8
    elif pct_ma >= 5:            trend_score = 8
    elif pct_ma >= 0:            trend_score = 5
    else:                        trend_score = 2

    total = vol_score + clarity_score + box_score + rr_score + trend_score

    if total >= 90:   grade, tier, emoji = "A+", "CLEAN",    "\U0001F7E2"
    elif total >= 85: grade, tier, emoji = "A",  "CLEAN",    "\U0001F7E2"
    elif total >= 75: grade, tier, emoji = "B+", "SOLID",    "\U0001F7E1"
    elif total >= 70: grade, tier, emoji = "B",  "SOLID",    "\U0001F7E1"
    elif total >= 60: grade, tier, emoji = "C+", "MARGINAL", "\U0001F7E0"
    elif total >= 55: grade, tier, emoji = "C",  "MARGINAL", "\U0001F7E0"
    else:             grade, tier, emoji = "D",  "WEAK",     "\U0001F534"

    return {
        "score": total, "grade": grade, "tier": tier, "emoji": emoji,
        "score_breakdown": {
            "volume": vol_score, "clarity": clarity_score,
            "box_quality": box_score, "risk_reward": rr_score,
            "trend": trend_score,
        },
    }


# ============================================================
# De-dup
# ============================================================
def load_recent_alerts():
    recent = set()
    path = script_path(DEDUP_FILE)
    if not os.path.exists(path):
        return recent
    cutoff = time.time() - (DEDUP_HOURS * 3600)
    try:
        with open(path, "r") as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) == 2:
                    ts, tkr = parts
                    if float(ts) >= cutoff:
                        recent.add(tkr)
    except Exception:
        pass
    return recent


def record_alerts(tickers):
    now = time.time()
    with open(script_path(DEDUP_FILE), "a") as f:
        for t in tickers:
            f.write(f"{now},{t}\n")


# ============================================================
# Telegram
# ============================================================
def send_telegram(message):
    if not SEND_TELEGRAM:
        print("  (Telegram env vars missing -- skipping push)")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200:
            print(f"  Telegram send failed ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        print(f"  Telegram send error: {e}")


def format_telegram_message(h):
    sb = h["score_breakdown"]
    return (
        f"{h['emoji']} *{h['tier']}* \u2022 *{h['ticker']}*  `${h['price']}`  "
        f"*[{h['grade']} \u2022 {h['score']}/100]*\n\n"
        f"*Score breakdown:*\n"
        f"\u2022 Volume conviction: `{sb['volume']}/20`  ({h['vol_ratio']}\u00D7 avg)\n"
        f"\u2022 Breakout clarity: `{sb['clarity']}/20`  (+{h['pct_above_box']}% above box)\n"
        f"\u2022 Box quality:      `{sb['box_quality']}/20`  ({h['box_range_pct']}% range)\n"
        f"\u2022 Risk/reward:      `{sb['risk_reward']}/20`  ({h['rr_ratio']}:1 R/R)\n"
        f"\u2022 Trend alignment:  `{sb['trend']}/20`  "
        f"({'rising' if h['ma_rising'] else 'flat/falling'} MA20)\n\n"
        f"*Trade plan:*\n"
        f"\u2022 Entry:  `${h['price']}`\n"
        f"\u2022 Stop:   `${h['suggested_stop']}`  \u2192 risk `{h['risk_pct']}%`\n"
        f"\u2022 Target: `${h['target_30pct']}`  \u2192 reward `+30%`\n\n"
        f"[Chart](https://finance.yahoo.com/quote/{h['ticker']})  "
        f"\u00B7  [Finviz](https://finviz.com/quote.ashx?t={h['ticker']})  "
        f"\u00B7  [TV](https://www.tradingview.com/symbols/{h['ticker']}/)"
    )


def format_summary_message(hits):
    lines = [f"\U0001F4CA *SCAN SUMMARY* \u2014 {len(hits)} breakout(s)\n"]
    for h in hits:
        lines.append(
            f"{h['emoji']} `{h['grade']:>2}` \u2022 *{h['ticker']:<5}* "
            f"`${h['price']:>6}` \u2022 score *{h['score']}*"
        )
    clean = [h for h in hits if h["tier"] == "CLEAN"]
    lines.append("")
    if clean:
        lines.append(f"\U0001F4A1 *Primary candidates:* "
                     + ", ".join(f"*{h['ticker']}*" for h in clean))
    else:
        lines.append("\u26A0 No CLEAN setups this run. Be selective.")
    return "\n".join(lines)


# ============================================================
# Dashboard output
# ============================================================
def write_dashboard_data(hits):
    now_iso = datetime.now().isoformat(timespec="seconds")
    payload = {
        "timestamp": now_iso,
        "count": len(hits),
        "hits": hits,
        "settings": {
            "box_days": DARVAS_BOX_DAYS,
            "vol_multiplier": VOLUME_MULTIPLIER,
            "min_price": MIN_PRICE,
            "max_price": MAX_PRICE,
            "sources": ["Finviz", "Yahoo"],
        },
    }
    with open(script_path(LATEST_JSON), "w") as f:
        json.dump(payload, f, indent=2)

    history = []
    hist_path = script_path(HISTORY_JSON)
    if os.path.exists(hist_path):
        try:
            with open(hist_path, "r") as f:
                history = json.load(f)
        except Exception:
            history = []
    history.insert(0, {
        "timestamp": now_iso,
        "count": len(hits),
        "tickers": [h["ticker"] for h in hits],
        "clean_count": sum(1 for h in hits if h["tier"] == "CLEAN"),
    })
    history = history[:50]
    with open(hist_path, "w") as f:
        json.dump(history, f, indent=2)


def print_results(hits):
    if not hits:
        print("\nNo clean breakout setups found. That's normal -- patience.")
        return
    print("\n" + "=" * 80)
    print(f"TTM BREAKOUT CANDIDATES  --  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 80)
    for h in hits:
        sb = h["score_breakdown"]
        print(f"\n  [{h['grade']:>2}] {h['tier']:<8} {h['ticker']:<5}  "
              f"${h['price']}  score {h['score']}/100")
        print(f"    vol:{sb['volume']}/20  clarity:{sb['clarity']}/20  "
              f"box:{sb['box_quality']}/20  R/R:{sb['risk_reward']}/20  "
              f"trend:{sb['trend']}/20")
        print(f"    Entry ${h['price']}  \u2192  Stop ${h['suggested_stop']} "
              f"({h['risk_pct']}%)  \u2192  Target ${h['target_30pct']} "
              f"(R/R {h['rr_ratio']}:1)")
    clean = [h for h in hits if h["tier"] == "CLEAN"]
    print("\n" + "-" * 80)
    if clean:
        print(f"PRIMARY CANDIDATES ({len(clean)}): "
              + ", ".join(h["ticker"] for h in clean))
    else:
        print("No CLEAN setups this run. Be selective.")
    print("=" * 80 + "\n")


# ============================================================
# MAIN
# ============================================================
def main():
    print("\n>>> TTM Breakout Scanner starting...\n")
    tickers = get_all_candidates()

    hits = []
    if tickers:
        print(f"Verifying Darvas breakouts via Yahoo Finance...\n")
        for i, ticker in enumerate(tickers, start=1):
            print(f"  [{i}/{len(tickers)}] {ticker}...", end=" ")
            result = check_darvas_breakout(ticker)
            if result:
                print(f"BREAKOUT  [{result['grade']}] {result['tier']}  "
                      f"score {result['score']}/100")
                hits.append(result)
            else:
                print("no")
            time.sleep(0.8)

    hits = sorted(hits, key=lambda x: (x["score"], x["vol_ratio"]), reverse=True)
    hits = hits[:TOP_N_TO_SHOW]

    print_results(hits)
    write_dashboard_data(hits)

    if hits and SEND_TELEGRAM:
        recent = load_recent_alerts()
        new_hits = [h for h in hits if h["ticker"] not in recent]
        if new_hits:
            print(f"Sending {len(new_hits)} alert(s) + summary to Telegram...")
            for h in new_hits:
                send_telegram(format_telegram_message(h))
                time.sleep(0.6)
            send_telegram(format_summary_message(new_hits))
            record_alerts([h["ticker"] for h in new_hits])
        else:
            print(f"(All {len(hits)} tickers already alerted in last {DEDUP_HOURS}h.)")

    if hits:
        df = pd.DataFrame([
            {k: v for k, v in h.items() if k not in ("spark", "score_breakdown")}
            for h in hits
        ])
        filename = script_path(f"breakouts_{datetime.now().strftime('%Y%m%d_%H%M')}.csv")
        df.to_csv(filename, index=False)
        print(f"Results saved to: {os.path.basename(filename)}\n")


if __name__ == "__main__":
    main()
