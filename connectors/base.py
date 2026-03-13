"""
Abstract base class for all platform connectors.
Every DEX connector must implement these methods.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from utils.models import (
    AccountBalance, FundingRate, Platform, Position,
    Side, TradeResult,
)


class BaseConnector(ABC):
    """Interface that each perpetual DEX connector must satisfy."""

    platform: Platform

    @abstractmethod
    async def initialise(self) -> None:
        """Perform async setup (SDK init, authentication, etc.)."""
        ...

    @abstractmethod
    async def shutdown(self) -> None:
        """Gracefully tear down connections."""
        ...

    # ── Market data ──────────────────────────────────────────────────────

    @abstractmethod
    async def get_available_symbols(self) -> List[str]:
        """Return normalised symbols this platform supports (e.g. ['BTC', 'ETH'])."""
        ...

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> Optional[FundingRate]:
        """Fetch the current funding rate for *symbol*."""
        ...

    @abstractmethod
    async def get_all_funding_rates(self) -> List[FundingRate]:
        """Fetch funding rates for all available perp markets."""
        ...

    @abstractmethod
    async def get_mark_price(self, symbol: str) -> Optional[float]:
        """Return the current mark / index price in USD."""
        ...

    # ── Account ──────────────────────────────────────────────────────────

    @abstractmethod
    async def get_balance(self) -> AccountBalance:
        """Return the account balance snapshot."""
        ...

    @abstractmethod
    async def get_open_positions(self) -> List[Dict]:
        """Return raw list of open positions on this platform."""
        ...

    # ── Trading ──────────────────────────────────────────────────────────

    @abstractmethod
    async def open_position(
        self,
        symbol: str,
        side: Side,
        size_usd: float,
        leverage: float = 1.0,
        max_slippage_pct: float = 0.5,
    ) -> TradeResult:
        """Open a new perpetual position."""
        ...

    @abstractmethod
    async def close_position(
        self,
        symbol: str,
        side: Side,
        size: Optional[float] = None,  # None = close full
    ) -> TradeResult:
        """Close an existing position (fully or partially)."""
        ...

    # ── Fees ─────────────────────────────────────────────────────────────

    @abstractmethod
    async def estimate_fees(self, symbol: str, size_usd: float) -> float:
        """Estimate round-trip trading fees in USD for a given notional size."""
        ...

    # ── Helpers ──────────────────────────────────────────────────────────

    def normalise_symbol(self, raw_symbol: str) -> str:
        """
        Convert platform-specific symbol to canonical form.
        Override in subclass if needed.
        e.g. 'BTCUSDT' -> 'BTC', 'BTC-PERP' -> 'BTC'
        """
        s = raw_symbol.upper()
        for suffix in ("USDT", "USD", "USDC", "-PERP", "_PERP", "/USD", "/USDT"):
            if s.endswith(suffix):
                s = s[: -len(suffix)]
        return s
