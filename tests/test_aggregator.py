"""Tests for the FundingRateAggregator."""

import pytest
from unittest.mock import patch

from engine.aggregator import FundingRateAggregator
from utils.models import FundingRate, Platform, PositionStatus
from tests.conftest import MockConnector, make_funding_rate, make_position


class TestRefreshAllRates:
    @pytest.mark.asyncio
    async def test_aggregates_rates_from_multiple_platforms(self):
        hl = MockConnector(Platform.HYPERLIQUID, funding_rates=[
            make_funding_rate(Platform.HYPERLIQUID, "BTC", 0.0001),
            make_funding_rate(Platform.HYPERLIQUID, "ETH", 0.0002),
        ])
        aster = MockConnector(Platform.ASTER, funding_rates=[
            make_funding_rate(Platform.ASTER, "BTC", 0.005),
            make_funding_rate(Platform.ASTER, "ETH", 0.004),
        ])
        agg = FundingRateAggregator([hl, aster])
        rates = await agg.refresh_all_rates()

        assert "BTC" in rates
        assert "ETH" in rates
        assert Platform.HYPERLIQUID in rates["BTC"]
        assert Platform.ASTER in rates["BTC"]

    @pytest.mark.asyncio
    async def test_last_update_set_after_refresh(self):
        hl = MockConnector(Platform.HYPERLIQUID, funding_rates=[
            make_funding_rate(Platform.HYPERLIQUID, "BTC", 0.001),
        ])
        agg = FundingRateAggregator([hl])
        assert agg.last_update is None
        await agg.refresh_all_rates()
        assert agg.last_update is not None

    @pytest.mark.asyncio
    async def test_filters_to_top_200(self):
        hl = MockConnector(Platform.HYPERLIQUID, funding_rates=[
            make_funding_rate(Platform.HYPERLIQUID, "BTC", 0.001),
            make_funding_rate(Platform.HYPERLIQUID, "FAKECOIN123", 0.01),
        ])
        agg = FundingRateAggregator([hl])
        rates = await agg.refresh_all_rates()

        assert "BTC" in rates
        assert "FAKECOIN123" not in rates

    @pytest.mark.asyncio
    async def test_handles_connector_error_gracefully(self):
        hl = MockConnector(Platform.HYPERLIQUID, funding_rates=[
            make_funding_rate(Platform.HYPERLIQUID, "BTC", 0.001),
        ])
        # Create a connector that raises on get_all_funding_rates
        bad = MockConnector(Platform.ASTER)
        async def _raise():
            raise Exception("API down")
        bad.get_all_funding_rates = _raise

        agg = FundingRateAggregator([hl, bad])
        rates = await agg.refresh_all_rates()
        assert "BTC" in rates

    @pytest.mark.asyncio
    async def test_clears_previous_rates_on_refresh(self):
        hl = MockConnector(Platform.HYPERLIQUID, funding_rates=[
            make_funding_rate(Platform.HYPERLIQUID, "BTC", 0.001),
        ])
        agg = FundingRateAggregator([hl])

        await agg.refresh_all_rates()
        assert "BTC" in agg.rates

        # Change rates to only ETH
        hl._funding_rates = [
            make_funding_rate(Platform.HYPERLIQUID, "ETH", 0.002),
        ]
        await agg.refresh_all_rates()
        assert "ETH" in agg.rates
        # BTC should be gone since rates are cleared on refresh
        assert "BTC" not in agg.rates


class TestFindOpportunities:
    def _setup_aggregator(self):
        """Create an aggregator with a large spread on BTC."""
        hl = MockConnector(Platform.HYPERLIQUID, funding_rates=[
            make_funding_rate(Platform.HYPERLIQUID, "BTC", 0.0001),  # low
        ])
        aster = MockConnector(Platform.ASTER, funding_rates=[
            make_funding_rate(Platform.ASTER, "BTC", 0.005),  # high
        ])
        return FundingRateAggregator([hl, aster])

    @pytest.mark.asyncio
    async def test_finds_opportunity_above_threshold(self):
        agg = self._setup_aggregator()
        await agg.refresh_all_rates()

        # BTC spread: (0.005 - 0.0001) * 8760 * 100 = ~4286% annualised
        opps = await agg.find_opportunities(entry_threshold_pct=25.0)
        assert len(opps) >= 1
        btc_opp = [o for o in opps if o.symbol == "BTC"][0]
        assert btc_opp.long_platform == Platform.HYPERLIQUID
        assert btc_opp.short_platform == Platform.ASTER
        assert btc_opp.spread_ann > 25.0

    @pytest.mark.asyncio
    async def test_no_opportunity_below_threshold(self):
        # Both platforms have similar rates
        hl = MockConnector(Platform.HYPERLIQUID, funding_rates=[
            make_funding_rate(Platform.HYPERLIQUID, "BTC", 0.001),
        ])
        aster = MockConnector(Platform.ASTER, funding_rates=[
            make_funding_rate(Platform.ASTER, "BTC", 0.00101),
        ])
        agg = FundingRateAggregator([hl, aster])
        await agg.refresh_all_rates()

        opps = await agg.find_opportunities(entry_threshold_pct=25.0)
        assert len(opps) == 0

    @pytest.mark.asyncio
    async def test_needs_at_least_two_platforms(self):
        hl = MockConnector(Platform.HYPERLIQUID, funding_rates=[
            make_funding_rate(Platform.HYPERLIQUID, "BTC", 0.01),
        ])
        agg = FundingRateAggregator([hl])
        await agg.refresh_all_rates()

        opps = await agg.find_opportunities(entry_threshold_pct=1.0)
        assert len(opps) == 0

    @pytest.mark.asyncio
    async def test_opportunities_sorted_by_spread_descending(self):
        hl = MockConnector(Platform.HYPERLIQUID, funding_rates=[
            make_funding_rate(Platform.HYPERLIQUID, "BTC", 0.0001),
            make_funding_rate(Platform.HYPERLIQUID, "ETH", 0.0001),
        ])
        aster = MockConnector(Platform.ASTER, funding_rates=[
            make_funding_rate(Platform.ASTER, "BTC", 0.003),   # smaller spread
            make_funding_rate(Platform.ASTER, "ETH", 0.006),   # larger spread
        ])
        agg = FundingRateAggregator([hl, aster])
        await agg.refresh_all_rates()

        opps = await agg.find_opportunities(entry_threshold_pct=1.0)
        assert len(opps) == 2
        assert opps[0].spread_ann >= opps[1].spread_ann

    @pytest.mark.asyncio
    async def test_long_platform_has_lowest_rate(self):
        agg = self._setup_aggregator()
        await agg.refresh_all_rates()
        opps = await agg.find_opportunities(entry_threshold_pct=1.0)

        for opp in opps:
            assert opp.long_rate_ann <= opp.short_rate_ann

    @pytest.mark.asyncio
    async def test_net_profit_accounts_for_fees(self):
        agg = self._setup_aggregator()
        await agg.refresh_all_rates()
        opps = await agg.find_opportunities(entry_threshold_pct=1.0)

        for opp in opps:
            assert opp.estimated_fees_usd > 0
            assert opp.net_profit_daily_usd < opp.estimated_profit_daily_usd


class TestFindExitCandidates:
    @pytest.mark.asyncio
    async def test_flags_position_when_spread_narrowed(self):
        # Current rates: spread is now very small
        # spread = (0.001001 - 0.001) * 8760 * 100 = 0.876% < 5% threshold
        hl = MockConnector(Platform.HYPERLIQUID, funding_rates=[
            make_funding_rate(Platform.HYPERLIQUID, "BTC", 0.001),
        ])
        aster = MockConnector(Platform.ASTER, funding_rates=[
            make_funding_rate(Platform.ASTER, "BTC", 0.001001),  # tiny spread
        ])
        agg = FundingRateAggregator([hl, aster])
        await agg.refresh_all_rates()

        pos = make_position(
            symbol="BTC",
            long_platform=Platform.HYPERLIQUID,
            short_platform=Platform.ASTER,
        )
        candidates = agg.find_exit_candidates([pos], exit_threshold_pct=5.0)
        assert len(candidates) == 1
        assert candidates[0].id == pos.id

    @pytest.mark.asyncio
    async def test_no_exit_when_spread_still_wide(self):
        hl = MockConnector(Platform.HYPERLIQUID, funding_rates=[
            make_funding_rate(Platform.HYPERLIQUID, "BTC", 0.0001),
        ])
        aster = MockConnector(Platform.ASTER, funding_rates=[
            make_funding_rate(Platform.ASTER, "BTC", 0.005),  # still wide
        ])
        agg = FundingRateAggregator([hl, aster])
        await agg.refresh_all_rates()

        pos = make_position(
            symbol="BTC",
            long_platform=Platform.HYPERLIQUID,
            short_platform=Platform.ASTER,
        )
        candidates = agg.find_exit_candidates([pos], exit_threshold_pct=5.0)
        assert len(candidates) == 0

    @pytest.mark.asyncio
    async def test_missing_rate_skips_position(self):
        hl = MockConnector(Platform.HYPERLIQUID, funding_rates=[
            make_funding_rate(Platform.HYPERLIQUID, "BTC", 0.001),
        ])
        # Aster has no BTC rate
        aster = MockConnector(Platform.ASTER, funding_rates=[])
        agg = FundingRateAggregator([hl, aster])
        await agg.refresh_all_rates()

        pos = make_position(
            symbol="BTC",
            long_platform=Platform.HYPERLIQUID,
            short_platform=Platform.ASTER,
        )
        candidates = agg.find_exit_candidates([pos], exit_threshold_pct=5.0)
        assert len(candidates) == 0


class TestGetRateSummary:
    @pytest.mark.asyncio
    async def test_returns_flat_list(self):
        hl = MockConnector(Platform.HYPERLIQUID, funding_rates=[
            make_funding_rate(Platform.HYPERLIQUID, "BTC", 0.001),
        ])
        aster = MockConnector(Platform.ASTER, funding_rates=[
            make_funding_rate(Platform.ASTER, "BTC", 0.002),
        ])
        agg = FundingRateAggregator([hl, aster])
        await agg.refresh_all_rates()

        summary = agg.get_rate_summary()
        assert len(summary) == 2
        assert all("symbol" in row for row in summary)
        assert all("platform" in row for row in summary)
        assert all("rate_hourly_pct" in row for row in summary)
        assert all("rate_ann_pct" in row for row in summary)
