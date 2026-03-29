"""
Position Manager.
Tracks all open arb position pairs, persists state to disk,
and handles opening/closing of paired positions.
"""

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from config import settings
from connectors.base import BaseConnector
from utils.logger import get_logger
from utils.models import (
    AccountBalance, Platform, Position, PositionStatus,
    Side, TradeResult,
)

log = get_logger("position_manager")

STATE_FILE = Path(__file__).parent.parent / "state" / "positions.json"


class PositionManager:
    """
    Manages the lifecycle of arbitrage position pairs.
    Each 'position' is a pair: long on one platform, short on another.
    """

    def __init__(self, connectors: Dict[Platform, BaseConnector]):
        self._connectors = connectors
        self._positions: Dict[str, Position] = {}
        self._load_state()

    # ── Persistence ──────────────────────────────────────────────────────

    def _load_state(self):
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                for pid, pdata in data.items():
                    pdata["long_platform"] = Platform(pdata["long_platform"])
                    pdata["short_platform"] = Platform(pdata["short_platform"])
                    pdata["status"] = PositionStatus(pdata["status"])
                    pdata["opened_at"] = datetime.fromisoformat(pdata["opened_at"])
                    if pdata.get("closed_at"):
                        pdata["closed_at"] = datetime.fromisoformat(pdata["closed_at"])
                    self._positions[pid] = Position(**pdata)
                log.info(f"Loaded {len(self._positions)} positions from state file")
            except Exception as e:
                log.error(f"Error loading state: {e}")

    def _save_state(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        for pid, pos in self._positions.items():
            d = {
                "id": pos.id,
                "symbol": pos.symbol,
                "long_platform": pos.long_platform.value,
                "short_platform": pos.short_platform.value,
                "side_long_size": pos.side_long_size,
                "side_short_size": pos.side_short_size,
                "entry_spread_ann": pos.entry_spread_ann,
                "entry_price": pos.entry_price,
                "notional_usd": pos.notional_usd,
                "status": pos.status.value,
                "opened_at": pos.opened_at.isoformat(),
                "closed_at": pos.closed_at.isoformat() if pos.closed_at else None,
                "pnl_usd": pos.pnl_usd,
                "fees_paid_usd": pos.fees_paid_usd,
                "long_order_id": pos.long_order_id,
                "short_order_id": pos.short_order_id,
            }
            data[pid] = d
        STATE_FILE.write_text(json.dumps(data, indent=2))

    # ── Queries ──────────────────────────────────────────────────────────

    @property
    def open_positions(self) -> List[Position]:
        return [p for p in self._positions.values() if p.status == PositionStatus.OPEN]

    @property
    def all_positions(self) -> List[Position]:
        return list(self._positions.values())

    def get_position(self, position_id: str) -> Optional[Position]:
        return self._positions.get(position_id)

    def count_open(self) -> int:
        return len(self.open_positions)

    def has_open_position(self, symbol: str) -> bool:
        return any(
            p.symbol == symbol and p.status == PositionStatus.OPEN
            for p in self._positions.values()
        )

    # ── Balance checks ───────────────────────────────────────────────────

    async def get_total_balance(self) -> float:
        """Sum equity across all platforms."""
        total = 0.0
        for platform, connector in self._connectors.items():
            try:
                bal = await connector.get_balance()
                total += bal.equity_usd
            except Exception as e:
                log.error(f"Balance check error on {platform.value}: {e}")
        return total

    async def check_balance_requirements(
        self,
        long_platform: Platform,
        short_platform: Platform,
        size_usd_per_leg: float,
    ) -> bool:
        """Verify both platforms have enough margin for the trade."""
        for platform in [long_platform, short_platform]:
            conn = self._connectors.get(platform)
            if not conn:
                log.error(f"No connector for {platform.value}")
                return False
            try:
                bal = await conn.get_balance()
                if bal.free_margin_usd < size_usd_per_leg:
                    log.warning(
                        f"Insufficient margin on {platform.value}: "
                        f"${bal.free_margin_usd:.2f} < ${size_usd_per_leg:.2f}"
                    )
                    return False
            except Exception as e:
                log.error(f"Balance check failed on {platform.value}: {e}")
                return False
        return True

    # ── Open a new arb pair ──────────────────────────────────────────────

    async def open_arb_position(
        self,
        symbol: str,
        long_platform: Platform,
        short_platform: Platform,
        size_usd: float,
        entry_spread_ann: float,
    ) -> Optional[Position]:
        """
        Open a pair of positions: long on one platform, short on another.
        Both positions have equal USD notional.
        """
        # Pre-flight checks
        if self.has_open_position(symbol):
            log.warning(f"Already have open position for {symbol}, skipping")
            return None

        if self.count_open() >= settings.arb.max_concurrent_positions:
            log.warning("Max concurrent positions reached, skipping")
            return None

        long_conn = self._connectors.get(long_platform)
        short_conn = self._connectors.get(short_platform)

        if not long_conn or not short_conn:
            log.error("Missing connector for one of the platforms")
            return None

        # Check balances
        can_trade = await self.check_balance_requirements(
            long_platform, short_platform, size_usd
        )
        if not can_trade:
            return None

        # Execute legs SEQUENTIALLY — place the more restrictive platform
        # first (Hyperliquid uses IOC orders that can fail on insufficient
        # margin).  Only proceed to the second leg if the first succeeds.
        # This prevents costly unwind trades when one leg fills and the
        # other doesn't.
        log.info(
            f"Opening arb: LONG {symbol} on {long_platform.value}, "
            f"SHORT on {short_platform.value}, ${size_usd:.2f}/leg"
        )

        # Determine execution order: Hyperliquid first (IOC = more likely
        # to fail), then the other platform.
        first_conn, first_side, first_platform = long_conn, Side.LONG, long_platform
        second_conn, second_side, second_platform = short_conn, Side.SHORT, short_platform

        if short_platform == Platform.HYPERLIQUID:
            first_conn, first_side, first_platform = short_conn, Side.SHORT, short_platform
            second_conn, second_side, second_platform = long_conn, Side.LONG, long_platform

        # --- Leg 1 ---
        try:
            first_result = await first_conn.open_position(
                symbol, first_side, size_usd,
                max_slippage_pct=settings.arb.max_slippage_pct,
            )
        except Exception as e:
            first_result = TradeResult(
                success=False, platform=first_platform, symbol=symbol,
                side=first_side, size=0, price=0, fee_usd=0,
                error=str(e),
            )

        if not first_result.success:
            log.error(
                f"First leg ({first_platform.value} {first_side.value}) failed "
                f"for {symbol}: {first_result.error} — skipping second leg entirely"
            )
            return None

        # --- Leg 2 (only reached if leg 1 succeeded) ---
        try:
            second_result = await second_conn.open_position(
                symbol, second_side, size_usd,
                max_slippage_pct=settings.arb.max_slippage_pct,
            )
        except Exception as e:
            second_result = TradeResult(
                success=False, platform=second_platform, symbol=symbol,
                side=second_side, size=0, price=0, fee_usd=0,
                error=str(e),
            )

        if not second_result.success:
            log.error(
                f"Second leg ({second_platform.value} {second_side.value}) failed "
                f"for {symbol}: {second_result.error} — unwinding first leg"
            )
            try:
                unwind = await first_conn.close_position(symbol, first_side, first_result.size)
                if unwind.success:
                    log.info(f"Unwind {first_platform.value} {first_side.value} succeeded for {symbol}")
                else:
                    log.error(
                        f"UNWIND FAILED for {symbol} {first_side.value} on {first_platform.value}: "
                        f"{unwind.error} — ORPHANED POSITION, manual close required"
                    )
            except Exception as e:
                log.error(
                    f"UNWIND EXCEPTION for {symbol} {first_side.value} on {first_platform.value}: "
                    f"{e} — ORPHANED POSITION, manual close required"
                )
            return None

        # Map results back to long/short
        if first_side == Side.LONG:
            long_result, short_result = first_result, second_result
        else:
            long_result, short_result = second_result, first_result

        # Record the position
        pos_id = f"arb_{symbol}_{uuid.uuid4().hex[:8]}"
        avg_price = (long_result.price + short_result.price) / 2

        position = Position(
            id=pos_id,
            symbol=symbol,
            long_platform=long_platform,
            short_platform=short_platform,
            side_long_size=long_result.size,
            side_short_size=short_result.size,
            entry_spread_ann=entry_spread_ann,
            entry_price=avg_price,
            notional_usd=size_usd,
            fees_paid_usd=long_result.fee_usd + short_result.fee_usd,
            long_order_id=long_result.order_id,
            short_order_id=short_result.order_id,
        )

        self._positions[pos_id] = position
        self._save_state()

        log.info(
            f"Opened arb position {pos_id}: {symbol} "
            f"L={long_platform.value} S={short_platform.value} "
            f"size=${size_usd:.2f} spread={entry_spread_ann:.2f}%"
        )
        return position

    # ── Close an arb pair ────────────────────────────────────────────────

    async def close_arb_position(self, position_id: str) -> bool:
        """Close both legs of an arb position."""
        pos = self._positions.get(position_id)
        if not pos or pos.status != PositionStatus.OPEN:
            log.warning(f"Position {position_id} not found or not open")
            return False

        pos.status = PositionStatus.CLOSING
        self._save_state()

        long_conn = self._connectors.get(pos.long_platform)
        short_conn = self._connectors.get(pos.short_platform)

        if not long_conn or not short_conn:
            log.error(f"Missing connectors for closing {position_id}")
            pos.status = PositionStatus.FAILED
            self._save_state()
            return False

        log.info(f"Closing arb position {position_id}: {pos.symbol}")

        long_close = long_conn.close_position(
            pos.symbol, Side.LONG, pos.side_long_size
        )
        short_close = short_conn.close_position(
            pos.symbol, Side.SHORT, pos.side_short_size
        )

        long_result, short_result = await asyncio.gather(
            long_close, short_close, return_exceptions=True
        )

        success = True
        total_fees = 0.0

        if isinstance(long_result, Exception) or not getattr(long_result, "success", False):
            log.error(f"Failed to close long leg: {long_result}")
            success = False
        else:
            total_fees += long_result.fee_usd

        if isinstance(short_result, Exception) or not getattr(short_result, "success", False):
            log.error(f"Failed to close short leg: {short_result}")
            success = False
        else:
            total_fees += short_result.fee_usd

        if success:
            pos.status = PositionStatus.CLOSED
            pos.closed_at = datetime.utcnow()
            pos.fees_paid_usd += total_fees
            # PnL will be calculated from on-chain data in a follow-up
            log.info(f"Successfully closed {position_id}")
        else:
            pos.status = PositionStatus.FAILED
            log.error(f"Partial close failure for {position_id}")

        self._save_state()
        return success

    # ── Stats ────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        open_pos = self.open_positions
        closed = [p for p in self._positions.values() if p.status == PositionStatus.CLOSED]
        total_pnl = sum(p.pnl_usd for p in closed)
        total_fees = sum(p.fees_paid_usd for p in self._positions.values())

        return {
            "open_positions": len(open_pos),
            "closed_positions": len(closed),
            "total_pnl_usd": total_pnl,
            "total_fees_usd": total_fees,
            "net_pnl_usd": total_pnl - total_fees,
            "positions": [
                {
                    "id": p.id,
                    "symbol": p.symbol,
                    "long": p.long_platform.value,
                    "short": p.short_platform.value,
                    "notional": p.notional_usd,
                    "entry_spread": p.entry_spread_ann,
                    "status": p.status.value,
                    "opened_at": p.opened_at.isoformat(),
                }
                for p in self._positions.values()
            ],
        }

