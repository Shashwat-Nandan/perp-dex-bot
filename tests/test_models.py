"""Tests for data models in utils/models.py"""

from datetime import datetime
from utils.models import (
    AccountBalance, ArbOpportunity, FundingRate, Platform,
    Position, PositionStatus, Side, TradeResult,
)


class TestEnums:
    def test_side_values(self):
        assert Side.LONG.value == "long"
        assert Side.SHORT.value == "short"

    def test_platform_values(self):
        assert len(Platform) == 6
        assert Platform.HYPERLIQUID.value == "hyperliquid"
        assert Platform.DRIFT.value == "drift"

    def test_platform_from_string(self):
        assert Platform("hyperliquid") == Platform.HYPERLIQUID
        assert Platform("aster") == Platform.ASTER

    def test_position_status_values(self):
        assert PositionStatus.OPEN.value == "open"
        assert PositionStatus.CLOSING.value == "closing"
        assert PositionStatus.CLOSED.value == "closed"
        assert PositionStatus.FAILED.value == "failed"


class TestFundingRate:
    def test_creation(self):
        fr = FundingRate(
            platform=Platform.HYPERLIQUID,
            symbol="BTC",
            rate_hourly=0.001,
            rate_annualised=0.001 * 8760,
        )
        assert fr.symbol == "BTC"
        assert fr.rate_hourly == 0.001
        assert fr.rate_annualised == 0.001 * 8760
        assert isinstance(fr.timestamp, datetime)

    def test_annualised_rate_calculation(self):
        hourly = 0.0005
        fr = FundingRate(
            platform=Platform.ASTER,
            symbol="ETH",
            rate_hourly=hourly,
            rate_annualised=hourly * 8760,
        )
        assert fr.rate_annualised == hourly * 8760

    def test_raw_field_default_none(self):
        fr = FundingRate(
            platform=Platform.LIGHTER,
            symbol="SOL",
            rate_hourly=0.0,
            rate_annualised=0.0,
        )
        assert fr.raw is None


class TestArbOpportunity:
    def test_creation(self):
        opp = ArbOpportunity(
            symbol="BTC",
            long_platform=Platform.HYPERLIQUID,
            short_platform=Platform.ASTER,
            long_rate_ann=5.0,
            short_rate_ann=35.0,
            spread_ann=30.0,
            estimated_profit_daily_usd=1.5,
            estimated_fees_usd=0.1,
            net_profit_daily_usd=1.4,
        )
        assert opp.spread_ann == 30.0
        assert opp.net_profit_daily_usd == 1.4
        assert isinstance(opp.detected_at, datetime)

    def test_spread_is_difference(self):
        opp = ArbOpportunity(
            symbol="ETH",
            long_platform=Platform.LIGHTER,
            short_platform=Platform.EDGEX,
            long_rate_ann=2.0,
            short_rate_ann=40.0,
            spread_ann=38.0,
            estimated_profit_daily_usd=2.0,
            estimated_fees_usd=0.2,
            net_profit_daily_usd=1.8,
        )
        assert opp.spread_ann == opp.short_rate_ann - opp.long_rate_ann


class TestPosition:
    def test_default_status_is_open(self):
        pos = Position(
            id="arb_BTC_abc123",
            symbol="BTC",
            long_platform=Platform.HYPERLIQUID,
            short_platform=Platform.ASTER,
            side_long_size=0.01,
            side_short_size=0.01,
            entry_spread_ann=30.0,
            entry_price=50000.0,
            notional_usd=500.0,
        )
        assert pos.status == PositionStatus.OPEN
        assert pos.pnl_usd == 0.0
        assert pos.fees_paid_usd == 0.0
        assert pos.closed_at is None

    def test_position_fields(self):
        pos = Position(
            id="arb_ETH_xyz789",
            symbol="ETH",
            long_platform=Platform.LIGHTER,
            short_platform=Platform.OSTIUM,
            side_long_size=0.5,
            side_short_size=0.5,
            entry_spread_ann=26.0,
            entry_price=3000.0,
            notional_usd=300.0,
            fees_paid_usd=0.6,
        )
        assert pos.symbol == "ETH"
        assert pos.notional_usd == 300.0


class TestTradeResult:
    def test_successful_trade(self):
        tr = TradeResult(
            success=True,
            platform=Platform.HYPERLIQUID,
            symbol="BTC",
            side=Side.LONG,
            size=0.01,
            price=50000.0,
            fee_usd=0.175,
            order_id="hl_order_123",
        )
        assert tr.success is True
        assert tr.error is None

    def test_failed_trade(self):
        tr = TradeResult(
            success=False,
            platform=Platform.ASTER,
            symbol="ETH",
            side=Side.SHORT,
            size=0,
            price=0,
            fee_usd=0,
            error="Insufficient margin",
        )
        assert tr.success is False
        assert "margin" in tr.error.lower()


class TestAccountBalance:
    def test_creation(self):
        bal = AccountBalance(
            platform=Platform.EDGEX,
            equity_usd=5000.0,
            free_margin_usd=3000.0,
            used_margin_usd=2000.0,
            unrealised_pnl_usd=-50.0,
        )
        assert bal.equity_usd == 5000.0
        assert bal.free_margin_usd + bal.used_margin_usd == bal.equity_usd
        assert isinstance(bal.timestamp, datetime)
