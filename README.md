# AlluSkyRocketStocks v2

Serverless EOD breakout system. Trend-gated Darvas trigger, RS-ranked,
SQLite-backed, single-file dashboard. Both markets (US + NSE), one codebase.

## Pipeline (GitHub Actions, free tier)
universe -> bulk history -> trend gate (4 checks) -> Darvas trigger (14d box, 2x vol)
-> RS percentile rank -> SQLite -> analyzer backfills 7/14/30d outcomes
-> data.json -> dashboard (GitHub Pages) + Telegram digest

## Setup on the existing repo
1. Copy these files over the repo root (legacy CSVs can stay until migration is confirmed)
2. Run once locally or via Actions:
   pip install -r requirements.txt
   python migrate.py US .            # absorbs breakouts_*.csv + alerted_history.json
   python migrate.py IN /path/to/clueless-indian-bot
3. Commit data/asr.db, data.json, dashboard.html
4. GitHub Pages already enabled -> dashboard.html is live
5. After confirming counts, delete legacy breakouts_*.csv (git history keeps them anyway)

## Hard rules (unchanged from v1)
1. 2% account risk per trade. Non-negotiable.
2. One position at a time at current account size.
3. Journal entry in trades.md after every closed trade.
4. No sizing without running the formula. The detail panel runs it for you —
   the numbers must match the journal.

## The edge report
The dashboard's bottom section is the verdict. Positive 30-day expectancy
after 50+ measured signals = the strategy earns real size. Until then,
the system is collecting evidence, not bragging rights.
