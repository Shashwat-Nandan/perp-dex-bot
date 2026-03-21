"""
Arbitrage Engine — the main orchestrator.
Ties together the aggregator and position manager,
runs the scan→decide→execute loop.
"""

import asyncio
from datetime import datetime
from typing import Dict, List, Optional

from config import settings
from connectors.base import BaseConnector
from utils.logger import get_logger
from utils.models import ArbOpportunity, Platform, Position
from .aggregator import FundingRateAggregator
from .position_manager import PositionManager

log = get_logger("arb_engine")


class ArbEngine:
    """
    Core arbitrage engine. Each `run_cycle()` call:
      1. Refreshes all funding rates
      2. Scans for new opportunities (spread > entry threshold)
      3. Opens new positions if balance allows
      4. Checks existing positions for exit signals (spread < exit threshold)
      5. Closes positions that meet exit criteria
    """

    def __init__(self, connectors: List[BaseConnector]):
        self._connector_map: Dict[Platform, BaseConnector] = {
            c.platform: c for c in connectors
        }
        self._aggregator = FundingRateAggregator(connectors)
        self._position_mgr = PositionManager(self._connector_map)
        self._cycle_count = 0
        self._last_cycle: Optional[datetime] = None
        self._last_opportunities: List[ArbOpportunity] = []

    @property
    def aggregator(self) -> FundingRateAggregator:
        return self._aggregator

    @property
    def position_manager(self) -> PositionManager:
        return self._position_mgr

    @property
    def last_opportunities(self) -> List[ArbOpportunity]:
        return self._last_opportunities

    # ── Main cycle ───────────────────────────────────────────────────────

    async def run_cycle(self) -> dict:
        """
        Execute one full scan-and-trade cycle.
        Returns a summary dict for logging / alerts.
        """
        self._cycle_count += 1
        cycle_start = datetime.utcnow()
        summary = {
            "cycle": self._cycle_count,
            "started_at": cycle_start.isoformat(),
            "new_opportunities": 0,
            "positions_opened": 0,
            "positions_closed": 0,
            "errors": [],
        }

        try:
            # 1. Refresh funding rates
            log.info(f"=== Cycle {self._cycle_count} starting ===")
            await self._aggregator.refresh_all_rates()

            # 2. Check for exit signals on open positions
            exits_closed = await self._check_exits()
            summary["positions_closed"] = exits_closed

            # 3. Scan for new opportunities
            # Compute estimated position size so profit estimates are realistic
            total_bal = await self._position_mgr.get_total_balance()
            est_notional = total_bal * (settings.arb.position_size_pct / 100) if total_bal > 0 else None
            opportunities = await self._aggregator.find_opportunities(
                notional_usd=est_notional,
            )
            self._last_opportunities = opportunities
            summary["new_opportunities"] = len(opportunities)

            if opportunities:
                log.info(f"Found {len(opportunities)} opportunities above {settings.arb.entry_rate_diff_pct}% threshold")
                for opp in opportunities[:5]:  # log top 5
                    log.info(
                        f"  {opp.symbol}: spread={opp.spread_ann:.2f}% "
                        f"L={opp.long_platform.value}({opp.long_rate_ann:.2f}%) "
                        f"S={opp.short_platform.value}({opp.short_rate_ann:.2f}%) "
                        f"est. daily=${opp.net_profit_daily_usd:.2f}"
                    )

            # 4. Open new positions
            opened = await self._open_new_positions(opportunities)
            summary["positions_opened"] = opened

        except Exception as e:
            log.error(f"Cycle error: {e}", exc_info=True)
            summary["errors"].append(str(e))

        self._last_cycle = datetime.utcnow()
        elapsed = (self._last_cycle - cycle_start).total_seconds()
        summary["elapsed_seconds"] = elapsed

        log.info(
            f"=== Cycle {self._cycle_count} complete in {elapsed:.1f}s: "
            f"{summary['new_opportunities']} opps, "
            f"{summary['positions_opened']} opened, "
            f"{summary['positions_closed']} closed ==="
        )
        return summary

    # ── Exit checks ──────────────────────────────────────────────────────

    async def _check_exits(self) -> int:
        """Check open positions for exit signals, close those that qualify."""
        open_positions = self._position_mgr.open_positions
        if not open_positions:
            return 0

        exit_candidates = self._aggregator.find_exit_candidates(open_positions)
        closed_count = 0

        for pos in exit_candidates:
            try:
                success = await self._position_mgr.close_arb_position(pos.id)
                if success:
                    closed_count += 1
                    log.info(f"Closed position {pos.id} ({pos.symbol})")
            except Exception as e:
                log.error(f"Error closing position {pos.id}: {e}")

        return closed_count

    # ── Open new positions ───────────────────────────────────────────────

    async def _open_new_positions(
        self, opportunities: List[ArbOpportunity]
    ) -> int:
        """
        Process opportunities and open positions for the best ones.
        Respects all risk controls.
        """
        opened = 0

        for opp in opportunities:
            # Skip if we already have a position for this symbol
            if self._position_mgr.has_open_position(opp.symbol):
                log.info(f"Skipping {opp.symbol}: already have an open position")
                continue

            # Skip if at max concurrent positions
            if self._position_mgr.count_open() >= settings.arb.max_concurrent_positions:
                log.info("Max positions reached, stopping new opens")
                break

            # Get per-platform balances for the two platforms involved
            long_conn = self._connector_map.get(opp.long_platform)
            short_conn = self._connector_map.get(opp.short_platform)

            if not long_conn or not short_conn:
                log.info(
                    f"Skipping {opp.symbol}: missing connector for "
                    f"{opp.long_platform.value} or {opp.short_platform.value}"
                )
                continue

            # Fetch balances from the two relevant platforms
            try:
                long_bal, short_bal = await asyncio.gather(
                    long_conn.get_balance(),
                    short_conn.get_balance(),
                )
            except Exception as e:
                log.warning(f"Skipping {opp.symbol}: balance fetch failed: {e}")
                continue

            log.info(
                f"Balance check for {opp.symbol}: "
                f"{opp.long_platform.value} equity=${long_bal.equity_usd:.2f} "
                f"free_margin=${long_bal.free_margin_usd:.2f} | "
                f"{opp.short_platform.value} equity=${short_bal.equity_usd:.2f} "
                f"free_margin=${short_bal.free_margin_usd:.2f}"
            )

            total_balance = long_bal.equity_usd + short_bal.equity_usd
            if total_balance < settings.arb.min_balance_usd:
                log.warning(
                    f"Total balance ${total_balance:.2f} below "
                    f"minimum ${settings.arb.min_balance_usd:.2f}"
                )
                break

            # Size per leg = min of the two platform balances * position_size_pct
            # This ensures the trade fits within the smaller platform's margin
            min_platform_balance = min(long_bal.free_margin_usd, short_bal.free_margin_usd)
            size_per_leg = min(
                total_balance * (settings.arb.position_size_pct / 100),
                min_platform_balance * 0.95,  # 95% of smaller platform to leave buffer
            )

            if size_per_leg <= 0:
                log.info(
                    f"Skipping {opp.symbol}: insufficient margin "
                    f"(long={opp.long_platform.value} ${long_bal.free_margin_usd:.2f}, "
                    f"short={opp.short_platform.value} ${short_bal.free_margin_usd:.2f})"
                )
                continue

            # Recalculate daily profit with the actual position size
            actual_daily_profit = (opp.spread_ann / 100 / 365) * size_per_leg

            # Estimate round-trip fees for both legs
            total_fees = size_per_leg * 0.002  # fallback: 0.1% per leg
            try:
                fee_long, fee_short = await asyncio.gather(
                    long_conn.estimate_fees(opp.symbol, size_per_leg),
                    short_conn.estimate_fees(opp.symbol, size_per_leg),
                )
                total_fees = fee_long + fee_short
            except Exception as e:
                log.warning(f"Fee estimation failed for {opp.symbol}: {e}")

            # Net daily profit after amortising fees over 30 days
            net_daily_profit = actual_daily_profit - (total_fees / 30)

            # Skip if net profit is negative
            if net_daily_profit <= 0:
                log.info(
                    f"Skipping {opp.symbol}: negative net profit "
                    f"(daily=${actual_daily_profit:.4f}, fees=${total_fees:.4f})"
                )
                continue

            # Check minimum profit threshold (monthly)
            if net_daily_profit * 30 < settings.arb.min_profit_threshold_usd:
                log.info(
                    f"Skipping {opp.symbol}: monthly profit ${net_daily_profit * 30:.2f} "
                    f"below ${settings.arb.min_profit_threshold_usd:.2f} threshold"
                )
                continue

            # Check that fees break even within 7 days
            days_to_breakeven = total_fees / net_daily_profit if net_daily_profit > 0 else 999
            if days_to_breakeven > 7:
                log.info(
                    f"Skipping {opp.symbol}: breakeven in {days_to_breakeven:.1f} days "
                    f"(fees=${total_fees:.2f})"
                )
                continue

            log.info(
                f"Opening {opp.symbol}: spread={opp.spread_ann:.2f}% "
                f"size=${size_per_leg:.2f} net_daily=${net_daily_profit:.2f} "
                f"breakeven={days_to_breakeven:.1f}d"
            )

            # Open the position
            position = await self._position_mgr.open_arb_position(
                symbol=opp.symbol,
                long_platform=opp.long_platform,
                short_platform=opp.short_platform,
                size_usd=size_per_leg,
                entry_spread_ann=opp.spread_ann,
            )

            if position:
                opened += 1

        return opened

    # ── Status ───────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        return {
            "cycle_count": self._cycle_count,
            "last_cycle": self._last_cycle.isoformat() if self._last_cycle else None,
            "rates_last_update": (
                self._aggregator.last_update.isoformat()
                if self._aggregator.last_update else None
            ),
            "open_positions": self._position_mgr.count_open(),
            "top_opportunities": [
                {
                    "symbol": o.symbol,
                    "spread_ann_pct": round(o.spread_ann, 2),
                    "long": o.long_platform.value,
                    "short": o.short_platform.value,
                    "net_daily_usd": round(o.net_profit_daily_usd, 2),
                }
                for o in self._last_opportunities[:10]
            ],
            "position_stats": self._position_mgr.get_stats(),
            "dry_run": settings.dry_run,
        }
