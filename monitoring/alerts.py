"""
Alert manager — sends notifications via Telegram and Discord.
"""

import asyncio
from datetime import datetime
from typing import Optional

import aiohttp

from config import settings
from utils.logger import get_logger
from utils.models import ArbOpportunity, Position

log = get_logger("alerts")


class AlertManager:
    """Sends alerts to Telegram and/or Discord."""

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._tg_token = settings.alerts.telegram_bot_token
        self._tg_chat = settings.alerts.telegram_chat_id
        self._discord_url = settings.alerts.discord_webhook_url

    async def start(self):
        self._session = aiohttp.ClientSession()

    async def stop(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Send methods ─────────────────────────────────────────────────────

    async def _send_telegram(self, text: str):
        if not self._tg_token or not self._tg_chat:
            return
        url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
        payload = {
            "chat_id": self._tg_chat,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status != 200:
                    log.warning(f"Telegram send failed: {resp.status}")
        except Exception as e:
            log.error(f"Telegram error: {e}")

    async def _send_discord(self, text: str):
        if not self._discord_url:
            return
        payload = {"content": text}
        try:
            async with self._session.post(self._discord_url, json=payload) as resp:
                if resp.status not in (200, 204):
                    log.warning(f"Discord send failed: {resp.status}")
        except Exception as e:
            log.error(f"Discord error: {e}")

    async def send(self, message: str):
        """Send a message to all configured channels."""
        await asyncio.gather(
            self._send_telegram(message),
            self._send_discord(message),
            return_exceptions=True,
        )

    # ── Formatted alerts ─────────────────────────────────────────────────

    async def alert_opportunity(self, opp: ArbOpportunity):
        msg = (
            f"*New Arb Opportunity*\n"
            f"Symbol: `{opp.symbol}`\n"
            f"Spread: `{opp.spread_ann:.2f}%` annualised\n"
            f"Long: `{opp.long_platform.value}` ({opp.long_rate_ann:.2f}%)\n"
            f"Short: `{opp.short_platform.value}` ({opp.short_rate_ann:.2f}%)\n"
            f"Est. daily profit: `${opp.net_profit_daily_usd:.2f}`"
        )
        await self.send(msg)

    async def alert_position_opened(self, pos: Position):
        msg = (
            f"*Position Opened*\n"
            f"ID: `{pos.id}`\n"
            f"Symbol: `{pos.symbol}`\n"
            f"Long: `{pos.long_platform.value}` | Short: `{pos.short_platform.value}`\n"
            f"Notional: `${pos.notional_usd:.2f}` per leg\n"
            f"Entry spread: `{pos.entry_spread_ann:.2f}%`\n"
            f"Fees: `${pos.fees_paid_usd:.2f}`"
        )
        await self.send(msg)

    async def alert_position_closed(self, pos: Position):
        msg = (
            f"*Position Closed*\n"
            f"ID: `{pos.id}`\n"
            f"Symbol: `{pos.symbol}`\n"
            f"PnL: `${pos.pnl_usd:.2f}`\n"
            f"Total fees: `${pos.fees_paid_usd:.2f}`\n"
            f"Duration: `{(pos.closed_at - pos.opened_at).total_seconds() / 3600:.1f}h`"
        )
        await self.send(msg)

    async def alert_cycle_summary(self, summary: dict):
        msg = (
            f"*Cycle #{summary['cycle']}*\n"
            f"Opportunities: `{summary['new_opportunities']}`\n"
            f"Opened: `{summary['positions_opened']}`\n"
            f"Closed: `{summary['positions_closed']}`\n"
            f"Elapsed: `{summary.get('elapsed_seconds', 0):.1f}s`"
        )
        if summary.get("errors"):
            msg += f"\nErrors: {len(summary['errors'])}"
        await self.send(msg)

    async def alert_error(self, error_msg: str):
        msg = f"*ERROR*\n```\n{error_msg[:500]}\n```"
        await self.send(msg)

    async def alert_low_balance(self, total_balance: float):
        msg = (
            f"*LOW BALANCE WARNING*\n"
            f"Total equity: `${total_balance:.2f}`\n"
            f"Minimum required: `${settings.arb.min_balance_usd:.2f}`\n"
            f"Bot will not open new positions."
        )
        await self.send(msg)
