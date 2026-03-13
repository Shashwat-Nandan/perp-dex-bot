"""
Shared data models used across the bot.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class Platform(str, Enum):
    HYPERLIQUID = "hyperliquid"
    LIGHTER = "lighter"
    OSTIUM = "ostium"
    ASTER = "aster"
    EDGEX = "edgex"
    DRIFT = "drift"


class PositionStatus(str, Enum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


@dataclass
class FundingRate:
    """Snapshot of a funding rate on a single platform."""
    platform: Platform
    symbol: str                 # normalised symbol e.g. "BTC", "ETH"
    rate_hourly: float          # hourly funding rate as decimal (0.01 = 1%)
    rate_annualised: float      # annualised = hourly * 8760
    timestamp: datetime = field(default_factory=datetime.utcnow)
    raw: Optional[dict] = None  # raw API response for debugging


@dataclass
class ArbOpportunity:
    """A detected funding-rate arbitrage opportunity."""
    symbol: str
    long_platform: Platform     # platform where we go long (lower/negative rate)
    short_platform: Platform    # platform where we go short (higher/positive rate)
    long_rate_ann: float
    short_rate_ann: float
    spread_ann: float           # short_rate_ann - long_rate_ann
    estimated_profit_daily_usd: float
    estimated_fees_usd: float
    net_profit_daily_usd: float
    detected_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Position:
    """Tracks an open arbitrage position (a pair of long + short legs)."""
    id: str                     # unique ID for this arb position pair
    symbol: str
    long_platform: Platform
    short_platform: Platform
    side_long_size: float       # position size in base asset
    side_short_size: float
    entry_spread_ann: float     # spread at entry
    entry_price: float          # approximate entry price of the asset
    notional_usd: float         # USD notional per leg
    status: PositionStatus = PositionStatus.OPEN
    opened_at: datetime = field(default_factory=datetime.utcnow)
    closed_at: Optional[datetime] = None
    pnl_usd: float = 0.0
    fees_paid_usd: float = 0.0
    long_order_id: Optional[str] = None
    short_order_id: Optional[str] = None


@dataclass
class TradeResult:
    """Result of a single trade execution."""
    success: bool
    platform: Platform
    symbol: str
    side: Side
    size: float
    price: float
    fee_usd: float
    order_id: Optional[str] = None
    error: Optional[str] = None
    raw: Optional[dict] = None


@dataclass
class AccountBalance:
    """Balance snapshot for a single platform."""
    platform: Platform
    equity_usd: float
    free_margin_usd: float
    used_margin_usd: float
    unrealised_pnl_usd: float
    timestamp: datetime = field(default_factory=datetime.utcnow)
