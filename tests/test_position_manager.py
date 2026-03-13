"""Tests for the PositionManager."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from engine.position_manager import PositionManager
from utils.models import Platform, PositionStatus, Side
from tests.conftest import MockConnector, make_funding_rate, make_position


@pytest.fixture
def connector_map():
    hl = MockConnector(Platform.HYPERLIQUID, funding_rates=[
        make_funding_rate(Platform.HYPERLIQUID, "BTC", 0.001),
    ])
    aster = MockConnector(Platform.ASTER, funding_rates=[
        make_funding_rate(Platform.ASTER, "BTC", 0.005),
    ])
    return {Platform.HYPERLIQUID: hl, Platform.ASTER: aster}


@pytest.fixture
def pm(connector_map, tmp_path):
    """PositionManager with state file pointing to tmp directory."""
    state_file = tmp_path / "positions.json"
    with patch("engine.position_manager.STATE_FILE", state_file):
        mgr = PositionManager(connector_map)
    mgr._state_file_patch = state_file
    return mgr


def _patch_state_file(pm, tmp_path=None):
    """Helper to patch STATE_FILE for save operations."""
    return patch(
        "engine.position_manager.STATE_FILE",
        pm._state_file_patch if hasattr(pm, "_state_file_patch") else tmp_path / "positions.json",
    )


class TestPositionQueries:
    def test_initially_empty(self, pm):
        assert pm.count_open() == 0
        assert pm.open_positions == []
        assert pm.all_positions == []

    def test_has_open_position_false_when_empty(self, pm):
        assert pm.has_open_position("BTC") is False

    def test_get_position_returns_none_for_unknown(self, pm):
        assert pm.get_position("nonexistent") is None


class TestOpenArbPosition:
    @pytest.mark.asyncio
    async def test_opens_position_successfully(self, connector_map, tmp_path):
        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            pm = PositionManager(connector_map)

            with patch("engine.position_manager.STATE_FILE", state_file):
                pos = await pm.open_arb_position(
                    symbol="BTC",
                    long_platform=Platform.HYPERLIQUID,
                    short_platform=Platform.ASTER,
                    size_usd=100.0,
                    entry_spread_ann=30.0,
                )

        assert pos is not None
        assert pos.symbol == "BTC"
        assert pos.long_platform == Platform.HYPERLIQUID
        assert pos.short_platform == Platform.ASTER
        assert pos.status == PositionStatus.OPEN
        assert pos.notional_usd == 100.0
        assert pos.fees_paid_usd > 0

    @pytest.mark.asyncio
    async def test_records_both_order_ids(self, connector_map, tmp_path):
        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            pm = PositionManager(connector_map)
            pos = await pm.open_arb_position(
                symbol="BTC",
                long_platform=Platform.HYPERLIQUID,
                short_platform=Platform.ASTER,
                size_usd=100.0,
                entry_spread_ann=30.0,
            )

        assert pos.long_order_id is not None
        assert pos.short_order_id is not None

    @pytest.mark.asyncio
    async def test_rejects_duplicate_symbol(self, connector_map, tmp_path):
        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            pm = PositionManager(connector_map)
            first = await pm.open_arb_position(
                "BTC", Platform.HYPERLIQUID, Platform.ASTER, 100.0, 30.0,
            )
            assert first is not None

            second = await pm.open_arb_position(
                "BTC", Platform.HYPERLIQUID, Platform.ASTER, 100.0, 30.0,
            )
            assert second is None

    @pytest.mark.asyncio
    async def test_rejects_when_max_positions_reached(self, connector_map, tmp_path):
        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            pm = PositionManager(connector_map)
            with patch("engine.position_manager.settings") as mock_settings:
                mock_settings.arb.max_concurrent_positions = 1
                mock_settings.arb.max_slippage_pct = 0.5

                await pm.open_arb_position(
                    "BTC", Platform.HYPERLIQUID, Platform.ASTER, 100.0, 30.0,
                )
                result = await pm.open_arb_position(
                    "ETH", Platform.HYPERLIQUID, Platform.ASTER, 100.0, 25.0,
                )
                assert result is None

    @pytest.mark.asyncio
    async def test_unwinds_long_on_short_failure(self, tmp_path):
        hl = MockConnector(Platform.HYPERLIQUID, trade_success=True)
        aster = MockConnector(Platform.ASTER, trade_success=False)
        connectors = {Platform.HYPERLIQUID: hl, Platform.ASTER: aster}

        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            pm = PositionManager(connectors)
            pos = await pm.open_arb_position(
                "BTC", Platform.HYPERLIQUID, Platform.ASTER, 100.0, 30.0,
            )

        assert pos is None
        # Long leg should have been unwound
        assert len(hl.close_position_calls) == 1
        assert hl.close_position_calls[0]["side"] == Side.LONG

    @pytest.mark.asyncio
    async def test_unwinds_short_on_long_failure(self, tmp_path):
        hl = MockConnector(Platform.HYPERLIQUID, trade_success=False)
        aster = MockConnector(Platform.ASTER, trade_success=True)
        connectors = {Platform.HYPERLIQUID: hl, Platform.ASTER: aster}

        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            pm = PositionManager(connectors)
            pos = await pm.open_arb_position(
                "BTC", Platform.HYPERLIQUID, Platform.ASTER, 100.0, 30.0,
            )

        assert pos is None
        assert len(aster.close_position_calls) == 1
        assert aster.close_position_calls[0]["side"] == Side.SHORT

    @pytest.mark.asyncio
    async def test_rejects_insufficient_margin(self, tmp_path):
        from utils.models import AccountBalance
        low_balance = AccountBalance(
            platform=Platform.HYPERLIQUID,
            equity_usd=50.0,
            free_margin_usd=10.0,  # Not enough for 100 USD
            used_margin_usd=40.0,
            unrealised_pnl_usd=0.0,
        )
        hl = MockConnector(Platform.HYPERLIQUID, balance=low_balance)
        aster = MockConnector(Platform.ASTER)
        connectors = {Platform.HYPERLIQUID: hl, Platform.ASTER: aster}

        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            pm = PositionManager(connectors)
            pos = await pm.open_arb_position(
                "BTC", Platform.HYPERLIQUID, Platform.ASTER, 100.0, 30.0,
            )
        assert pos is None


class TestCloseArbPosition:
    @pytest.mark.asyncio
    async def test_closes_position_successfully(self, connector_map, tmp_path):
        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            pm = PositionManager(connector_map)
            pos = await pm.open_arb_position(
                "BTC", Platform.HYPERLIQUID, Platform.ASTER, 100.0, 30.0,
            )
            assert pos is not None

            success = await pm.close_arb_position(pos.id)

        assert success is True
        closed_pos = pm.get_position(pos.id)
        assert closed_pos.status == PositionStatus.CLOSED
        assert closed_pos.closed_at is not None

    @pytest.mark.asyncio
    async def test_close_nonexistent_returns_false(self, connector_map, tmp_path):
        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            pm = PositionManager(connector_map)
            result = await pm.close_arb_position("nonexistent_id")
        assert result is False

    @pytest.mark.asyncio
    async def test_close_failure_marks_as_failed(self, tmp_path):
        hl = MockConnector(Platform.HYPERLIQUID, trade_success=True)
        aster = MockConnector(Platform.ASTER, trade_success=True)
        connectors = {Platform.HYPERLIQUID: hl, Platform.ASTER: aster}

        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            pm = PositionManager(connectors)
            pos = await pm.open_arb_position(
                "BTC", Platform.HYPERLIQUID, Platform.ASTER, 100.0, 30.0,
            )

            # Now make close fail
            hl._trade_success = False
            aster._trade_success = False
            success = await pm.close_arb_position(pos.id)

        assert success is False
        assert pm.get_position(pos.id).status == PositionStatus.FAILED


class TestStatePersistence:
    @pytest.mark.asyncio
    async def test_saves_and_loads_state(self, connector_map, tmp_path):
        state_file = tmp_path / "state" / "positions.json"

        with patch("engine.position_manager.STATE_FILE", state_file):
            pm1 = PositionManager(connector_map)
            pos = await pm1.open_arb_position(
                "BTC", Platform.HYPERLIQUID, Platform.ASTER, 100.0, 30.0,
            )
            assert pos is not None
            assert state_file.exists()

        # Load into a new manager
        with patch("engine.position_manager.STATE_FILE", state_file):
            pm2 = PositionManager(connector_map)

        assert pm2.count_open() == 1
        loaded = pm2.get_position(pos.id)
        assert loaded.symbol == "BTC"
        assert loaded.status == PositionStatus.OPEN

    @pytest.mark.asyncio
    async def test_handles_corrupt_state_file(self, connector_map, tmp_path):
        state_file = tmp_path / "positions.json"
        state_file.write_text("not valid json!!!")

        with patch("engine.position_manager.STATE_FILE", state_file):
            pm = PositionManager(connector_map)

        assert pm.count_open() == 0


class TestGetStats:
    @pytest.mark.asyncio
    async def test_stats_structure(self, connector_map, tmp_path):
        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            pm = PositionManager(connector_map)
            stats = pm.get_stats()

        assert "open_positions" in stats
        assert "closed_positions" in stats
        assert "total_pnl_usd" in stats
        assert "total_fees_usd" in stats
        assert "net_pnl_usd" in stats
        assert "positions" in stats

    @pytest.mark.asyncio
    async def test_stats_after_open_and_close(self, connector_map, tmp_path):
        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            pm = PositionManager(connector_map)
            pos = await pm.open_arb_position(
                "BTC", Platform.HYPERLIQUID, Platform.ASTER, 100.0, 30.0,
            )
            stats = pm.get_stats()
            assert stats["open_positions"] == 1

            await pm.close_arb_position(pos.id)
            stats = pm.get_stats()
            assert stats["open_positions"] == 0
            assert stats["closed_positions"] == 1
            assert stats["total_fees_usd"] > 0


class TestBalanceChecks:
    @pytest.mark.asyncio
    async def test_get_total_balance(self, connector_map, tmp_path):
        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            pm = PositionManager(connector_map)
            total = await pm.get_total_balance()
        # Each MockConnector has 1000 equity, 2 connectors
        assert total == 2000.0

    @pytest.mark.asyncio
    async def test_check_balance_requirements_pass(self, connector_map, tmp_path):
        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            pm = PositionManager(connector_map)
            result = await pm.check_balance_requirements(
                Platform.HYPERLIQUID, Platform.ASTER, 100.0,
            )
        assert result is True

    @pytest.mark.asyncio
    async def test_check_balance_requirements_fail(self, connector_map, tmp_path):
        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            pm = PositionManager(connector_map)
            # Request more than free margin (800)
            result = await pm.check_balance_requirements(
                Platform.HYPERLIQUID, Platform.ASTER, 900.0,
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_check_balance_missing_connector(self, tmp_path):
        hl = MockConnector(Platform.HYPERLIQUID)
        connectors = {Platform.HYPERLIQUID: hl}  # No aster

        state_file = tmp_path / "positions.json"
        with patch("engine.position_manager.STATE_FILE", state_file):
            pm = PositionManager(connectors)
            result = await pm.check_balance_requirements(
                Platform.HYPERLIQUID, Platform.ASTER, 100.0,
            )
        assert result is False
