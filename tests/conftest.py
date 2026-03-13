"""
Shared fixtures for the perp_arb_bot test suite.
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from connectors.base import BaseConnector
from utils.models import (
    AccountBalance, ArbOpportunity, FundingRate, Platform,
    Position, PositionStatus, Side, TradeResult,
)


class MockConnector(BaseConnector):
    """A fully controllable mock connector for testing."""

    def __init__(
        self,
        platform: Platform,
        funding_rates: List[FundingRate] = None,
        balance: AccountBalance = None,
        trade_success: bool = True,
    ):
        self.platform = platform
        self._funding_rates = funding_rates or []
        self._balance = balance or AccountBalance(
            platform=platform,
            equity_usd=1000.0,
            free_margin_usd=800.0,
            used_margin_usd=200.0,
            unrealised_pnl_usd=0.0,
        )
        self._trade_success = trade_success
        self._symbols = list({fr.symbol for fr in self._funding_rates})
        self.open_position_calls = []
        self.close_position_calls = []

    async def initialise(self):
        pass

    async def shutdown(self):
        pass

    async def get_available_symbols(self) -> List[str]:
        return self._symbols

    async def get_funding_rate(self, symbol: str) -> Optional[FundingRate]:
        for fr in self._funding_rates:
            if fr.symbol == symbol:
                return fr
        return None

    async def get_all_funding_rates(self) -> List[FundingRate]:
        return self._funding_rates

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        return 50000.0 if symbol == "BTC" else 3000.0

    async def get_balance(self) -> AccountBalance:
        return self._balance

    async def get_open_positions(self) -> List[Dict]:
        return []

    async def open_position(
        self, symbol, side, size_usd, leverage=1.0, max_slippage_pct=0.5
    ) -> TradeResult:
        self.open_position_calls.append({
            "symbol": symbol, "side": side, "size_usd": size_usd,
        })
        if self._trade_success:
            price = 50000.0 if symbol == "BTC" else 3000.0
            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=side, size=size_usd / price, price=price,
                fee_usd=size_usd * 0.001, order_id=f"mock_{symbol}_{side.value}",
            )
        return TradeResult(
            success=False, platform=self.platform, symbol=symbol,
            side=side, size=0, price=0, fee_usd=0,
            error="Mock trade failure",
        )

    async def close_position(self, symbol, side, size=None) -> TradeResult:
        self.close_position_calls.append({
            "symbol": symbol, "side": side, "size": size,
        })
        if self._trade_success:
            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=side, size=size or 0.1, price=50000.0,
                fee_usd=0.05, order_id=f"mock_close_{symbol}",
            )
        return TradeResult(
            success=False, platform=self.platform, symbol=symbol,
            side=side, size=0, price=0, fee_usd=0,
            error="Mock close failure",
        )

    async def estimate_fees(self, symbol: str, size_usd: float) -> float:
        return size_usd * 0.001


def make_funding_rate(
    platform: Platform, symbol: str, hourly: float
) -> FundingRate:
    return FundingRate(
        platform=platform,
        symbol=symbol,
        rate_hourly=hourly,
        rate_annualised=hourly * 8760,
        timestamp=datetime.utcnow(),
    )


def make_position(
    symbol: str = "BTC",
    long_platform: Platform = Platform.HYPERLIQUID,
    short_platform: Platform = Platform.ASTER,
    status: PositionStatus = PositionStatus.OPEN,
    notional: float = 100.0,
) -> Position:
    return Position(
        id=f"arb_{symbol}_test1234",
        symbol=symbol,
        long_platform=long_platform,
        short_platform=short_platform,
        side_long_size=0.002,
        side_short_size=0.002,
        entry_spread_ann=30.0,
        entry_price=50000.0,
        notional_usd=notional,
        status=status,
        fees_paid_usd=0.20,
    )


@pytest.fixture
def mock_hl_connector():
    rates = [
        make_funding_rate(Platform.HYPERLIQUID, "BTC", 0.0001),
        make_funding_rate(Platform.HYPERLIQUID, "ETH", 0.0002),
    ]
    return MockConnector(Platform.HYPERLIQUID, funding_rates=rates)


@pytest.fixture
def mock_aster_connector():
    rates = [
        make_funding_rate(Platform.ASTER, "BTC", 0.005),
        make_funding_rate(Platform.ASTER, "ETH", 0.004),
    ]
    return MockConnector(Platform.ASTER, funding_rates=rates)


@pytest.fixture
def mock_lighter_connector():
    rates = [
        make_funding_rate(Platform.LIGHTER, "BTC", 0.001),
        make_funding_rate(Platform.LIGHTER, "ETH", 0.0015),
    ]
    return MockConnector(Platform.LIGHTER, funding_rates=rates)
