"""Tests for the AlertManager."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timedelta

from monitoring.alerts import AlertManager
from utils.models import ArbOpportunity, Platform, Position, PositionStatus


@pytest.fixture
def alert_mgr():
    with patch("monitoring.alerts.settings") as mock_settings:
        mock_settings.alerts.telegram_bot_token = "fake_token"
        mock_settings.alerts.telegram_chat_id = "123456"
        mock_settings.alerts.discord_webhook_url = "https://discord.com/api/webhooks/fake"
        mock_settings.arb.min_balance_usd = 150.0
        mgr = AlertManager()
        mgr._tg_token = "fake_token"
        mgr._tg_chat = "123456"
        mgr._discord_url = "https://discord.com/api/webhooks/fake"
    return mgr


class TestAlertManager:
    @pytest.mark.asyncio
    async def test_start_creates_session(self, alert_mgr):
        await alert_mgr.start()
        assert alert_mgr._session is not None
        await alert_mgr.stop()

    @pytest.mark.asyncio
    async def test_stop_closes_session(self, alert_mgr):
        await alert_mgr.start()
        await alert_mgr.stop()
        assert alert_mgr._session.closed

    @pytest.mark.asyncio
    async def test_send_telegram_skips_without_token(self):
        with patch("monitoring.alerts.settings") as mock_settings:
            mock_settings.alerts.telegram_bot_token = ""
            mock_settings.alerts.telegram_chat_id = ""
            mock_settings.alerts.discord_webhook_url = ""
            mgr = AlertManager()
            mgr._tg_token = ""
            mgr._tg_chat = ""
            mgr._discord_url = ""
            await mgr.start()
            # Should not raise
            await mgr._send_telegram("test")
            await mgr.stop()

    @pytest.mark.asyncio
    async def test_send_discord_skips_without_url(self):
        with patch("monitoring.alerts.settings") as mock_settings:
            mock_settings.alerts.telegram_bot_token = ""
            mock_settings.alerts.telegram_chat_id = ""
            mock_settings.alerts.discord_webhook_url = ""
            mgr = AlertManager()
            mgr._tg_token = ""
            mgr._tg_chat = ""
            mgr._discord_url = ""
            await mgr.start()
            await mgr._send_discord("test")
            await mgr.stop()


class TestAlertFormatting:
    @pytest.mark.asyncio
    async def test_alert_opportunity_format(self, alert_mgr):
        alert_mgr.send = AsyncMock()
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
        await alert_mgr.alert_opportunity(opp)

        alert_mgr.send.assert_called_once()
        msg = alert_mgr.send.call_args[0][0]
        assert "BTC" in msg
        assert "30.00%" in msg
        assert "hyperliquid" in msg
        assert "aster" in msg

    @pytest.mark.asyncio
    async def test_alert_position_opened_format(self, alert_mgr):
        alert_mgr.send = AsyncMock()
        pos = Position(
            id="arb_BTC_test1234",
            symbol="BTC",
            long_platform=Platform.HYPERLIQUID,
            short_platform=Platform.ASTER,
            side_long_size=0.002,
            side_short_size=0.002,
            entry_spread_ann=30.0,
            entry_price=50000.0,
            notional_usd=100.0,
            fees_paid_usd=0.20,
        )
        await alert_mgr.alert_position_opened(pos)

        msg = alert_mgr.send.call_args[0][0]
        assert "arb_BTC_test1234" in msg
        assert "100.00" in msg

    @pytest.mark.asyncio
    async def test_alert_position_closed_format(self, alert_mgr):
        alert_mgr.send = AsyncMock()
        now = datetime.utcnow()
        pos = Position(
            id="arb_BTC_test1234",
            symbol="BTC",
            long_platform=Platform.HYPERLIQUID,
            short_platform=Platform.ASTER,
            side_long_size=0.002,
            side_short_size=0.002,
            entry_spread_ann=30.0,
            entry_price=50000.0,
            notional_usd=100.0,
            status=PositionStatus.CLOSED,
            opened_at=now - timedelta(hours=5),
            closed_at=now,
            pnl_usd=15.0,
            fees_paid_usd=0.40,
        )
        await alert_mgr.alert_position_closed(pos)

        msg = alert_mgr.send.call_args[0][0]
        assert "15.00" in msg
        assert "5.0h" in msg

    @pytest.mark.asyncio
    async def test_alert_cycle_summary_format(self, alert_mgr):
        alert_mgr.send = AsyncMock()
        summary = {
            "cycle": 42,
            "new_opportunities": 3,
            "positions_opened": 1,
            "positions_closed": 0,
            "elapsed_seconds": 2.5,
            "errors": [],
        }
        await alert_mgr.alert_cycle_summary(summary)

        msg = alert_mgr.send.call_args[0][0]
        assert "#42" in msg
        assert "3" in msg

    @pytest.mark.asyncio
    async def test_alert_cycle_summary_with_errors(self, alert_mgr):
        alert_mgr.send = AsyncMock()
        summary = {
            "cycle": 1,
            "new_opportunities": 0,
            "positions_opened": 0,
            "positions_closed": 0,
            "elapsed_seconds": 1.0,
            "errors": ["Connection timeout"],
        }
        await alert_mgr.alert_cycle_summary(summary)

        msg = alert_mgr.send.call_args[0][0]
        assert "Errors" in msg

    @pytest.mark.asyncio
    async def test_alert_error_truncates_long_messages(self, alert_mgr):
        alert_mgr.send = AsyncMock()
        long_error = "x" * 1000
        await alert_mgr.alert_error(long_error)

        msg = alert_mgr.send.call_args[0][0]
        # Error message gets truncated to 500 chars
        assert len(msg) < 1000

    @pytest.mark.asyncio
    async def test_alert_low_balance(self, alert_mgr):
        alert_mgr.send = AsyncMock()
        await alert_mgr.alert_low_balance(50.0)

        msg = alert_mgr.send.call_args[0][0]
        assert "LOW BALANCE" in msg
        assert "50.00" in msg
