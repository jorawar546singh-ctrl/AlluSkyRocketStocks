"""
TTM Streak Tracker -- monitors continuation on recently-alerted tickers
========================================================================
Patched 2026-06-06:
  - NaN-safe JSON writer (the bug that broke the dashboard's Trending Watchlist)
  - today_alerted flag on each entry (powers the FRESH badge in dashboard)
"""
import os, json, time, math
from datetime import datetime, timedelta
import requests
import yfinance as yf
import pandas as pd


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
SEND_TELEGRAM = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ALERTED_HISTORY = "alerted_history.json"
STREAKS_STATE   = "streaks_state.json"

VOLUME_MULT_FOR_CONTINUATION = 1.5
RECENT_BOX_DAYS = 5
MILESTONE_DAYS  = [3, 5, 7, 10, 15, 20]
MAX_RED_DAYS_BEFORE_DROP = 5
DAYS_AFTER_TREND_END = 30


def script_path(name):
    return os.path.join(SCRIPT_DIR, name)


def send_telegram(message):
    if not SEND_TELEGRAM:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                  "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=10)
        if r.status_code != 200:
            print(f"  Telegram send failed ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        print(f"  Telegram send error: {e}")


def load_json(path, default):
    p = script_path(path)
    if not os.path.exists(p):
        return default
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return default


def _clean_for_json(obj):
    """
    Recursively replace NaN/Infinity with None so the dashboard's
    JSON.parse() doesn't choke. THIS IS THE BUG THAT BROKE CLUELESS TONIGHT.
    Apply this to anything we write to disk.
    """
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _clean_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_for_json(v) for v in obj]
    return obj


def save_json(path, data):
    cleaned = _clean_for_json(data)
    with open(script_path(path), "w") as f:
        json.dump(cleaned, f, indent=2, default=str)


def fetch_history(ticker, days=45):
    try:
        data = yf.download(ticker, period=f"{days}d", interval="1d",
                           progress=False, auto_adjust=False)
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)
        return data if data is not None and not data.empty else None
    except Exception:
        return None


def analyze_streak(ticker, breakout_entry, data):
    breakout_price = breakout_entry["breakout_price"]
    breakout_date_str = breakout_entry["first_alerted"][:10]

    try:
        breakout_date = datetime.fromisoformat(breakout_date_str)
    except Exception:
        breakout_date = datetime.now() - timedelta(days=1)

    data_post = data[data.index.date > breakout_date.date()]
    if data_post.empty:
        return None

    today_row = data_post.iloc[-1]
    today_close = float(today_row["Close"])
    today_volume = float(today_row["Volume"])

    if math.isnan(today_close):
        return None

    if len(data_post) >= 2:
        prior_post = data_post.iloc[:-1]
        post_breakout_high = float(prior_post["Close"].max())
    else:
        post_breakout_high = breakout_price

    is_new_post_breakout_high = today_close > post_breakout_high

    vol_lookback = min(20, len(data) - 1)
    avg_vol = float(data["Volume"].iloc[-(vol_lookback + 1):-1].mean()) if vol_lookback > 0 else 0
    vol_ratio = today_volume / avg_vol if avg_vol > 0 else 0
    volume_ok = vol_ratio >= VOLUME_MULT_FOR_CONTINUATION

    if len(data) >= RECENT_BOX_DAYS + 1:
        recent_box = data.iloc[-(RECENT_BOX_DAYS + 1):-1]
        recent_box_top = float(recent_box["High"].max())
        new_box_break = today_close > recent_box_top
    else:
        recent_box_top = None
        new_box_break = False

    streak = 0
    closes = data_post["Close"].tolist()
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] > closes[i - 1]:
            streak += 1
        else:
            break
    if streak == 0 and len(closes) >= 1 and closes[-1] > breakout_price:
        streak = 1

    days_since_breakout = len(data_post)

    red_streak = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            red_streak += 1
        else:
            break

    pct_from_breakout = (today_close - breakout_price) / breakout_price * 100

    return {
        "today_close": round(today_close, 2),
        "today_volume": int(today_volume),
        "vol_ratio": round(vol_ratio, 2),
        "volume_ok": volume_ok,
        "recent_box_top": round(recent_box_top, 2) if recent_box_top else None,
        "new_box_break": new_box_break,
        "post_breakout_high": round(post_breakout_high, 2),
        "is_new_post_breakout_high": is_new_post_breakout_high,
        "streak": streak,
        "days_since_breakout": days_since_breakout,
        "red_streak": red_streak,
        "pct_from_breakout": round(pct_from_breakout, 2),
    }


def format_trending_alert(ticker, be, st, milestone):
    sym = ticker
    warning = ""
    if milestone >= 10:
        warning = ("\n\u26A0 *Late-streak warning:* Extended from entry. "
                   "Late-entry R/R is poor. If not in, skip. If holding, let trailing stop work.")
    return (
        f"\U0001F525 *TRENDING: {sym}* \u2022 Day {milestone} streak\n\n"
        f"\u2022 Originally alerted *{be.get('original_grade', '?')}* "
        f"({be.get('original_tier', '?')}) on {be['first_alerted'][:10]}\n"
        f"\u2022 Breakout: `${be['breakout_price']:.2f}` \u2192 "
        f"now `${st['today_close']:.2f}` "
        f"(*{st['pct_from_breakout']:+.1f}%*)\n"
        f"\u2022 New post-breakout high \u2705\n"
        f"\u2022 Volume: *{st['vol_ratio']}\u00D7* avg \u2705\n"
        f"\u2022 Broke new 5-day box \u2705\n"
        f"\u2022 Streak: {st['streak']} consecutive up-days"
        f"{warning}\n\n"
        f"[Chart](https://finance.yahoo.com/quote/{sym})  "
        f"\u00B7  [Finviz](https://finviz.com/quote.ashx?t={sym})  "
        f"\u00B7  [TV](https://www.tradingview.com/symbols/{sym}/)"
    )


def format_fading_alert(ticker, be, st, peak_streak):
    sym = ticker
    return (
        f"\u26A0 *TREND FADING: {sym}*\n\n"
        f"\u2022 Peak streak: {peak_streak} days\n"
        f"\u2022 Post-breakout high: `${st['post_breakout_high']:.2f}`\n"
        f"\u2022 Today: `${st['today_close']:.2f}` "
        f"({st['pct_from_breakout']:+.1f}% from entry)\n\n"
        f"Not a sell signal by itself. If holding: check your trailing stop."
    )


def main():
    print("\n>>> TTM Streak Tracker starting...\n")

    alerted = load_json(ALERTED_HISTORY, [])
    if not alerted:
        print("No alerted tickers to track yet.\n")
        save_json(STREAKS_STATE, {"updated": datetime.now().isoformat(timespec="seconds"),
                                   "streaks": []})
        return

    prev_state = load_json(STREAKS_STATE, {"streaks": []})
    prev_by_ticker = {s["ticker"]: s for s in prev_state.get("streaks", [])}

    new_streaks = []
    alerts = []

    for entry in alerted:
        ticker = entry["ticker"]
        try:
            datetime.fromisoformat(entry["first_alerted"])
        except Exception:
            continue

        prev = prev_by_ticker.get(ticker, {})
        prev_status = prev.get("status", "fresh")
        trend_end_str = prev.get("trend_end_date")

        if trend_end_str and prev_status != "trending":
            try:
                trend_end = datetime.fromisoformat(trend_end_str)
                if (datetime.now() - trend_end).days > DAYS_AFTER_TREND_END:
                    print(f"  {ticker}... dropping (no trend for {DAYS_AFTER_TREND_END}+ days)")
                    continue
            except Exception:
                pass

        print(f"  {ticker}...", end=" ")
        data = fetch_history(ticker)
        if data is None:
            print("data unavailable")
            # Carry forward prior state so the ticker doesn't drop on a data hiccup
            if prev:
                new_streaks.append(prev)
            continue

        st = analyze_streak(ticker, entry, data)
        if not st:
            print("not enough post-breakout data")
            if prev:
                new_streaks.append(prev)
            continue

        if st["red_streak"] >= MAX_RED_DAYS_BEFORE_DROP:
            print(f"dropping (red streak {st['red_streak']})")
            continue

        last_milestone = prev.get("last_milestone_alerted", 0)
        peak_streak = max(prev.get("peak_streak", 0), st["streak"])
        prev_trend_end = prev.get("trend_end_date")

        status = "trending"
        new_trend_end = prev_trend_end

        if st["streak"] >= 2 or st["is_new_post_breakout_high"]:
            status = "trending"
            new_trend_end = None
        elif st["red_streak"] >= 1 and peak_streak >= 5 and prev_status == "trending":
            status = "fading"
            new_trend_end = datetime.now().isoformat(timespec="seconds")
            alerts.append(format_fading_alert(ticker, entry, st, peak_streak))
        else:
            status = prev_status if prev_status in ("trending", "fading") else "fresh"
            if prev_status == "trending" and status != "trending" and not prev_trend_end:
                new_trend_end = datetime.now().isoformat(timespec="seconds")

        fired_milestone = None
        if (st["is_new_post_breakout_high"] and st["volume_ok"] and st["new_box_break"]):
            for m in MILESTONE_DAYS:
                if st["streak"] >= m > last_milestone:
                    fired_milestone = m
                    alerts.append(format_trending_alert(ticker, entry, st, m))

        # Flag if this ticker was first alerted today (used by dashboard FRESH badge + top-pin)
        try:
            first_dt = datetime.fromisoformat(entry["first_alerted"])
            today_alerted = first_dt.date() == datetime.now().date()
        except Exception:
            today_alerted = False

        new_streaks.append({
            "ticker": ticker,
            "breakout_price": entry["breakout_price"],
            "first_alerted": entry["first_alerted"],
            "today_alerted": today_alerted,
            "original_grade": entry.get("original_grade"),
            "original_tier": entry.get("original_tier"),
            "days_since_breakout": st["days_since_breakout"],
            "today_close": st["today_close"],
            "pct_from_breakout": st["pct_from_breakout"],
            "post_breakout_high": st["post_breakout_high"],
            "current_streak": st["streak"],
            "peak_streak": peak_streak,
            "last_milestone_alerted": fired_milestone or last_milestone,
            "volume_ok_today": st["volume_ok"],
            "vol_ratio": st["vol_ratio"],
            "new_box_break_today": st["new_box_break"],
            "red_streak": st["red_streak"],
            "status": status,
            "trend_end_date": new_trend_end,
        })
        tag = "🔥" if status == "trending" else "⚠️" if status == "fading" else "…"
        print(f"{tag} day {st['days_since_breakout']}, streak {st['streak']}, "
              f"{st['pct_from_breakout']:+.1f}%")

    save_json(STREAKS_STATE, {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "streaks": new_streaks,
    })

    if alerts:
        print(f"\nSending {len(alerts)} streak alert(s) to Telegram...")
        for msg in alerts:
            send_telegram(msg)
            time.sleep(0.6)

    print("\n" + "=" * 70)
    print(f"STREAK TRACKER  --  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 70)


if __name__ == "__main__":
    main()
