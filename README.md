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

---

# Data files

| file | holds | grows |
|---|---|---|
| `data.json` | signals still **open** — what you decide from today | bounded |
| `history.json` | signals **closed** — what the system has actually produced | forever |

Both carry a top-level `generated` (UTC ISO-8601), rewritten on every run.
All field names are `snake_case`.

Signals are **never deleted**. The old rule dropped a signal once it stopped
trending, so the record quietly rewrote itself down to survivors and any
win/loss count taken from it was wrong by construction. A signal now moves
from `data.json` to `history.json` with a reason attached, and stays there.
`history.json` is merged by signal `id`, so re-exporting can never duplicate
a row or lose one.

## Close lifecycle

| field | type | meaning |
|---|---|---|
| `close_date` | `YYYY-MM-DD` \| null | null while open |
| `close_reason` | enum \| null | `stopped_out` · `expired` · `faded` · `hit_target_10` |
| `exit_price` | float \| null | the stop for `stopped_out`, else last close |
| `final_return_pct` | float \| null | `exit_price` vs signal price |
| `hit10_before_stop` | bool \| null | did +10% arrive **before** the stop was touched |
| `hit10_date` | `YYYY-MM-DD` \| null | when +10% was reached |

Reasons are checked in order, most specific first: `stopped_out` →
`expired` (age ≥ 180d) → `faded` (age > 30d and still `FADING`).

**`hit_target_10` does not close a signal by default.** Reaching +10% is
recorded as a milestone and tracking continues. The measured reason:

> 58% of signals touch +10% within 180 days. Of those, the average move went
> on to offer **+38%**, and 15 reached +100% or more — HCWB +759%,
> MRNA +337%, WFF +312%.

Closing at the target files every one of those as a +10% win and stops
watching, which is the exact failure this lifecycle exists to end. Flip
`CLOSE_ON_TARGET = True` in `core/lifecycle.py` to restore target closes; the
enum value is kept for that reason and for rows already closed that way.

### Two pairs of extremes, and why both

| field | window | answers |
|---|---|---|
| `max_gain_pct` / `max_dd_pct` | signal → **exit bar** | what the *trade* would have been |
| `peak_gain_pct` / `trough_dd_pct` | whole tracking window, exit ignored | what the *scanner* identified |

Conflating these is how a 312% move gets filed as a 7% loss. WFF stopped out
early and then ran: `max_gain_pct` says the trade lost, `peak_gain_pct` says
the signal was right and the stop was too tight. Only both together tell you
which one to fix. All four are intraday highs/lows, not closes.

Once a signal closes, these freeze. For the signal-identified peak over a full
180 days regardless of close state, use `outcomes.max_gain_d180`, which
`analyzer.py` backfills independently.

A close reason that asserts event *order* requires `walk_complete` — that the
price frame actually reached back to the signal date. Price history is fetched
over a fixed window, so an old signal can arrive with its first months
missing, and without them a stop touched in week three is invisible while a
later recovery through the target reads as a clean win. Those signals fall
through to `expired` rather than being credited a win or a loss the data
cannot place in time.

## Daily statistics
Computed from bars the scan already holds — no extra requests.

| field | notes |
|---|---|
| `prev_close`, `close` | last two closes |
| `day_change_pct` | close vs prev_close |
| `volume` | last session |
| `avg_volume_50d` | **excludes today**, so a huge session can't inflate its own baseline |
| `vol_ratio_50d` | `volume / avg_volume_50d`. Steadier than the existing 20-day `vol_ratio`, which one busy week moves; both are kept |
| `high_52w` | 252 sessions, excluding today |
| `dist_52w_high_pct` | close vs `high_52w`, negative = below |
| `new_52w_high` | today's high ≥ `high_52w` |

## Company info

| field | source |
|---|---|
| `company_name` | Finviz quote page |
| `sector`, `industry` | universe page where it carries them, Finviz quote page otherwise |
| `next_earnings_date` | Finviz. Year is inferred: the date is always the *next* report, so a month earlier than today resolves to next year |
| `days_to_earnings` | derived, negative once passed |

Looked up **per signal**, not per universe ticker: the universe runs to 400
names a day and signals to a handful, so doing this per ticker would mean 400
extra requests per run for data that only matters once a breakout fires. All
fields fail soft to null — company metadata is context, never a reason to drop
a signal or fail a run. US only; Finviz does not meaningfully cover NSE or TSX.

Historical US rows from before 2026-10-13 have null `sector`/`industry`.

## Buyout flag

| field | meaning |
|---|---|
| `pinned_buyout` | bool, the chart shape of a pending cash acquisition |
| `buyout_price` | always null — see below |

Matched when all three hold: a session gapped up ≥20% within the last 60
sessions, every session since has held inside a 2% band, and that band sits
at ≥98% of the 52-week high.

This is a **pattern match, not a fact**. A real acquisition flag needs a
corporate-actions feed, which this project does not have, and no free source
carries the offer price — hence `buyout_price` stays null rather than carrying
a guess. A stock can print this shape with no deal behind it.

## Box history
`boxes` table, one row per Darvas box per ticker:
`market`, `ticker`, `box_top`, `box_bottom`, `formed_date`,
`broken_date` (null while intact), unique on `(market, ticker, formed_date)`.
A box is marked broken when a close exceeds `box_top`.

## Run records
`runs` table gains `run_type`: `intraday` (19:00 UTC) or `close`
(21:30 UTC ≈ 16:30 ET). This is what the dashboard's staleness badge reads —
not `MAX(scan_date)`, which is when something last *fired*, not when the
scanner last *ran*. A healthy market with no breakouts looked broken before
this existed.
