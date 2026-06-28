"""
Market configuration. One codebase, N markets.
Every market-specific value lives here — scanners and trackers stay market-agnostic.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MarketConfig:
    key: str                 # "US" | "IN"
    label: str
    currency: str
    ticker_suffix: str       # "" for US, ".NS" for NSE
    benchmark: str           # index proxy for relative strength
    min_price: float
    max_price: float
    darvas_box_days: int = 14
    volume_multiplier: float = 2.0
    rs_lookback_days: int = 63        # ~3 months of trading days
    trend_near_high_pct: float = 25.0  # must be within X% of 52w high
    top_n: int = 12


US = MarketConfig(
    key="US", label="United States", currency="$", ticker_suffix="",
    benchmark="SPY", min_price=2.0, max_price=1000.0,
)

IN = MarketConfig(
    key="IN", label="India / NSE", currency="\u20b9", ticker_suffix=".NS",
    benchmark="^NSEI", min_price=50.0, max_price=5000.0, top_n=15,
)

MARKETS = {m.key: m for m in (US, IN)}

DB_PATH = "data/asr.db"
DASHBOARD_JSON = "data.json"
