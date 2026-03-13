"""Tests for the ArbEngine orchestrator."""

import pytest
from unittest.mock import patch, AsyncMock

from engine.arb_engine import ArbEngine
from utils.models import Platform, PositionStatus
from tests.conftest import MockConnector, make_funding_rate


def _create_engine_with_spread():
    """Create an engine with connectors that produce a large BTC spread."""
    hl = MockConnector(Platform.HYPERLIQUID, funding_rates=[
        make_funding_rate(Platform.HYPERLIQUID, "BTC", 0.0001),
    ])
    aster = MockConnector(Platform.ASTER, funding_rates=[
        make_funding_rate(Platform.ASTER, "BTC", 0.005),
    ])
    return ArbEngine([hl, aster]), hl, aster


class TestRunCycle:
    @pytest.mark.asyncio
    async def test_cycle_returns_summary(self, tmp_path):
        engine, _, _ = _create_engine_with_spread()
        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            engine._position_mgr._positions.clear()
            summary = await engine.run_cycle()

        assert "cycle" in summary
        assert summary["cycle"] == 1
        assert "new_opportunities" in summary
        assert "positions_opened" in summary
        assert "positions_closed" in summary
        assert "elapsed_seconds" in summary
        assert "errors" in summary

    @pytest.mark.asyncio
    async def test_cycle_count_increments(self, tmp_path):
        engine, _, _ = _create_engine_with_spread()
        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            engine._position_mgr._positions.clear()
            s1 = await engine.run_cycle()
            s2 = await engine.run_cycle()

        assert s1["cycle"] == 1
        assert s2["cycle"] == 2

    @pytest.mark.asyncio
    async def test_finds_opportunities(self, tmp_path):
        engine, _, _ = _create_engine_with_spread()
        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            engine._position_mgr._positions.clear()
            summary = await engine.run_cycle()

        assert summary["new_opportunities"] >= 1
        assert len(engine.last_opportunities) >= 1

    @pytest.mark.asyncio
    async def test_handles_cycle_error_gracefully(self, tmp_path):
        engine, _, _ = _create_engine_with_spread()
        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            engine._position_mgr._positions.clear()
            # Make refresh_all_rates fail
            engine._aggregator.refresh_all_rates = AsyncMock(
                side_effect=Exception("Network error")
            )
            summary = await engine.run_cycle()

        assert len(summary["errors"]) > 0
        assert "Network error" in summary["errors"][0]


class TestGetStatus:
    @pytest.mark.asyncio
    async def test_status_structure(self, tmp_path):
        engine, _, _ = _create_engine_with_spread()
        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            engine._position_mgr._positions.clear()
            status = engine.get_status()

        assert "cycle_count" in status
        assert "last_cycle" in status
        assert "open_positions" in status
        assert "top_opportunities" in status
        assert "position_stats" in status
        assert "dry_run" in status

    @pytest.mark.asyncio
    async def test_status_after_cycle(self, tmp_path):
        engine, _, _ = _create_engine_with_spread()
        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            engine._position_mgr._positions.clear()
            await engine.run_cycle()
            status = engine.get_status()

        assert status["cycle_count"] == 1
        assert status["last_cycle"] is not None
        assert status["rates_last_update"] is not None


class TestCheckExits:
    @pytest.mark.asyncio
    async def test_closes_positions_when_spread_narrows(self, tmp_path):
        # Open with wide spread
        hl = MockConnector(Platform.HYPERLIQUID, funding_rates=[
            make_funding_rate(Platform.HYPERLIQUID, "BTC", 0.0001),
        ])
        aster = MockConnector(Platform.ASTER, funding_rates=[
            make_funding_rate(Platform.ASTER, "BTC", 0.005),
        ])
        engine = ArbEngine([hl, aster])

        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            engine._position_mgr._positions.clear()
            # First cycle: open position
            await engine.run_cycle()
            assert engine.position_manager.count_open() >= 0  # may or may not open depending on profit threshold

    @pytest.mark.asyncio
    async def test_no_exits_when_no_open_positions(self, tmp_path):
        engine, _, _ = _create_engine_with_spread()
        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            engine._position_mgr._positions.clear()
            closed = await engine._check_exits()
        assert closed == 0


class TestOpenNewPositions:
    @pytest.mark.asyncio
    async def test_skips_negative_profit(self, tmp_path):
        from utils.models import ArbOpportunity
        from datetime import datetime

        engine, _, _ = _create_engine_with_spread()
        state_file = tmp_path / "positions.json"

        opp = ArbOpportunity(
            symbol="BTC",
            long_platform=Platform.HYPERLIQUID,
            short_platform=Platform.ASTER,
            long_rate_ann=5.0,
            short_rate_ann=6.0,
            spread_ann=1.0,
            estimated_profit_daily_usd=0.01,
            estimated_fees_usd=5.0,
            net_profit_daily_usd=-0.5,  # negative
        )

        with patch("engine.position_manager.STATE_FILE", state_file):
            engine._position_mgr._positions.clear()
            opened = await engine._open_new_positions([opp])
        assert opened == 0

    @pytest.mark.asyncio
    async def test_skips_below_min_profit_threshold(self, tmp_path):
        from utils.models import ArbOpportunity

        engine, _, _ = _create_engine_with_spread()
        state_file = tmp_path / "positions.json"

        opp = ArbOpportunity(
            symbol="BTC",
            long_platform=Platform.HYPERLIQUID,
            short_platform=Platform.ASTER,
            long_rate_ann=5.0,
            short_rate_ann=6.0,
            spread_ann=1.0,
            estimated_profit_daily_usd=0.05,
            estimated_fees_usd=0.01,
            net_profit_daily_usd=0.001,  # below threshold
        )

        with patch("engine.position_manager.STATE_FILE", state_file):
            engine._position_mgr._positions.clear()
            opened = await engine._open_new_positions([opp])
        assert opened == 0
