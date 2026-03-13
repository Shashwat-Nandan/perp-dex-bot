"""Tests for connector base class and symbol normalisation."""

import pytest
from connectors.base import BaseConnector
from utils.models import Platform, Side
from tests.conftest import MockConnector, make_funding_rate


class TestBaseConnectorNormalise:
    """Test the symbol normalisation logic in BaseConnector."""

    def setup_method(self):
        self.conn = MockConnector(Platform.HYPERLIQUID)

    def test_normalise_usdt_suffix(self):
        assert self.conn.normalise_symbol("BTCUSDT") == "BTC"

    def test_normalise_usd_suffix(self):
        assert self.conn.normalise_symbol("ETHUSD") == "ETH"

    def test_normalise_usdc_suffix(self):
        assert self.conn.normalise_symbol("SOLUSDC") == "SOL"

    def test_normalise_perp_suffix(self):
        assert self.conn.normalise_symbol("BTC-PERP") == "BTC"
        assert self.conn.normalise_symbol("ETH_PERP") == "ETH"

    def test_normalise_slash_suffix(self):
        assert self.conn.normalise_symbol("BTC/USD") == "BTC"
        assert self.conn.normalise_symbol("ETH/USDT") == "ETH"

    def test_normalise_already_clean(self):
        assert self.conn.normalise_symbol("BTC") == "BTC"

    def test_normalise_lowercase_input(self):
        assert self.conn.normalise_symbol("btcusdt") == "BTC"

    def test_normalise_mixed_case(self):
        assert self.conn.normalise_symbol("Eth-Perp") == "ETH"


class TestMockConnector:
    """Verify the MockConnector works correctly for test infrastructure."""

    @pytest.mark.asyncio
    async def test_get_all_funding_rates(self):
        rates = [
            make_funding_rate(Platform.HYPERLIQUID, "BTC", 0.001),
            make_funding_rate(Platform.HYPERLIQUID, "ETH", 0.002),
        ]
        conn = MockConnector(Platform.HYPERLIQUID, funding_rates=rates)
        result = await conn.get_all_funding_rates()
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_get_balance(self):
        conn = MockConnector(Platform.ASTER)
        bal = await conn.get_balance()
        assert bal.equity_usd == 1000.0
        assert bal.platform == Platform.ASTER

    @pytest.mark.asyncio
    async def test_open_position_success(self):
        conn = MockConnector(Platform.HYPERLIQUID, trade_success=True)
        result = await conn.open_position("BTC", Side.LONG, 100.0)
        assert result.success is True
        assert result.fee_usd == 0.1  # 100 * 0.001
        assert len(conn.open_position_calls) == 1

    @pytest.mark.asyncio
    async def test_open_position_failure(self):
        conn = MockConnector(Platform.HYPERLIQUID, trade_success=False)
        result = await conn.open_position("BTC", Side.LONG, 100.0)
        assert result.success is False
        assert result.error == "Mock trade failure"

    @pytest.mark.asyncio
    async def test_estimate_fees(self):
        conn = MockConnector(Platform.LIGHTER)
        fee = await conn.estimate_fees("BTC", 1000.0)
        assert fee == 1.0  # 1000 * 0.001

    @pytest.mark.asyncio
    async def test_initialise_and_shutdown(self):
        conn = MockConnector(Platform.EDGEX)
        await conn.initialise()
        await conn.shutdown()
