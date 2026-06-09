"""
SQLite storage. One file, committed to the repo by the Actions workflow.
Replaces the loose breakouts_*.csv + scattered JSON state files.

Tables
------
signals   : every breakout the scanner has ever fired (US + IN, market column)
outcomes  : forward returns per signal, backfilled by analyzer.py
positions : open/closed positions (migrated from positions.json)
"""
import os
import sqlite3
from contextlib import contextmanager

from core.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY,
    market        TEXT NOT NULL,            -- 'US' | 'IN'
    ticker        TEXT NOT NULL,            -- bare symbol, no suffix
    scan_ts       TEXT NOT NULL,            -- ISO timestamp of the scan
    scan_date     TEXT NOT NULL,            -- YYYY-MM-DD (dedup key component)
    price         REAL NOT NULL,
    box_top       REAL,
    box_bottom    REAL,
    pct_above_box REAL,
    vol_ratio     REAL,
    suggested_stop REAL,
    risk_pct      REAL,
    rr_ratio      REAL,
    -- v2 factors (NULL on migrated legacy rows)
    rs_pct        REAL,                      -- relative-strength percentile 0-100
    rs_excess     REAL,                      -- 63d return minus benchmark, pct pts
    trend_pass    INTEGER,                   -- 0/1, all four trend checks
    trend_detail  TEXT,                      -- e.g. '4/4: >MA50,MA50>MA200,MA200up,nearHigh'
    dist_52w_high REAL,                      -- % below 52-week high
    -- legacy fields kept for the historical record
    legacy_score  INTEGER,
    legacy_grade  TEXT,
    legacy_tier   TEXT,
    source        TEXT DEFAULT 'scanner',    -- 'scanner' | 'migration_csv' | 'migration_history'
    UNIQUE (market, ticker, scan_date)
);

CREATE TABLE IF NOT EXISTS outcomes (
    signal_id     INTEGER PRIMARY KEY REFERENCES signals(id),
    ret_d7        REAL,    -- % return 7 calendar days after signal
    ret_d14       REAL,
    ret_d30       REAL,
    max_gain_d30  REAL,    -- best intraday-high gain within 30d
    max_dd_d30    REAL,    -- worst intraday-low drawdown within 30d
    stop_hit_d30  INTEGER, -- 1 if low breached suggested_stop within 30d
    computed_at   TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id           INTEGER PRIMARY KEY,
    market       TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    shares       REAL NOT NULL,
    entry_price  REAL NOT NULL,
    entry_date   TEXT NOT NULL,
    initial_stop REAL NOT NULL,
    current_stop REAL,
    running_high REAL,
    status       TEXT DEFAULT 'open',   -- 'open' | 'stopped' | 'closed'
    exit_price   REAL,
    exit_date    TEXT,
    notes        TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_market_date ON signals (market, scan_date);
"""


@contextmanager
def connect(path: str = DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def upsert_signal(con, row: dict) -> int:
    """Insert a signal; on (market,ticker,scan_date) conflict keep the earliest, return its id."""
    cols = ",".join(row)
    ph = ",".join("?" * len(row))
    con.execute(
        f"INSERT INTO signals ({cols}) VALUES ({ph}) "
        f"ON CONFLICT(market,ticker,scan_date) DO NOTHING",
        list(row.values()),
    )
    cur = con.execute(
        "SELECT id FROM signals WHERE market=? AND ticker=? AND scan_date=?",
        (row["market"], row["ticker"], row["scan_date"]),
    )
    return cur.fetchone()["id"]
