"""
Drift Protocol connector (Solana-based perp DEX).
Uses driftpy SDK for all operations.
Note: Requires separate Solana wallet (not EVM).
"""

import asyncio
from typing import Dict, List, Optional

from config import settings
from utils.logger import get_logger
from utils.models import (
    AccountBalance, FundingRate, Platform, Side, TradeResult,
)
from .base import BaseConnector

log = get_logger("connector.drift")


class DriftConnector(BaseConnector):
    platform = Platform.DRIFT

    def __init__(self):
        self._drift_client = None
        self._drift_user = None
        self._connection = None
        self._wallet = None
        self._market_map: Dict[str, int] = {}  # symbol -> market_index
        self._symbols: List[str] = []

    async def initialise(self) -> None:
        if not settings.wallet.solana_private_key:
            log.warning("No Solana private key configured; Drift connector disabled")
            return

        try:
            from solders.keypair import Keypair
            from solana.rpc.async_api import AsyncClient
            from driftpy.drift_client import DriftClient
            from driftpy.drift_user import DriftUser
            from driftpy.types import MarketType
            from anchorpy import Wallet

            # Decode base58 private key
            import base58
            secret_bytes = base58.b58decode(settings.wallet.solana_private_key)
            kp = Keypair.from_bytes(secret_bytes)
            self._wallet = Wallet(kp)

            self._connection = AsyncClient(settings.rpc.solana)
            self._drift_client = DriftClient(
                self._connection,
                self._wallet,
                "mainnet",
            )
            await self._drift_client.subscribe()

            self._drift_user = DriftUser(self._drift_client)

            # Load perp markets
            perp_markets = self._drift_client.get_perp_market_accounts()
            for market in perp_markets:
                sym = self._decode_market_name(market.name)
                self._market_map[sym] = market.market_index
                self._symbols.append(sym)

            log.info(f"Drift initialised – {len(self._symbols)} perp markets")

        except ImportError as e:
            log.warning(f"driftpy not installed: {e}. Drift connector disabled.")
        except Exception as e:
            log.error(f"Drift initialisation error: {e}")

    async def shutdown(self) -> None:
        try:
            if self._drift_client:
                await self._drift_client.unsubscribe()
            if self._connection:
                await self._connection.close()
        except Exception as e:
            log.debug(f"Drift shutdown: {e}")

    @staticmethod
    def _decode_market_name(name_bytes) -> str:
        """Decode market name from bytes to string."""
        if isinstance(name_bytes, bytes):
            return name_bytes.decode("utf-8").strip("\x00").replace("-PERP", "").upper()
        return str(name_bytes).replace("-PERP", "").upper()

    async def get_available_symbols(self) -> List[str]:
        return self._symbols

    async def get_funding_rate(self, symbol: str) -> Optional[FundingRate]:
        if not self._drift_client:
            return None
        try:
            market_idx = self._market_map.get(symbol.upper())
            if market_idx is None:
                return None

            from driftpy.constants.numeric_constants import FUNDING_RATE_PRECISION

            market = self._drift_client.get_perp_market_account(market_idx)
            # last_funding_rate is in FUNDING_RATE_PRECISION units
            last_rate = market.amm.last_funding_rate / FUNDING_RATE_PRECISION
            rate_hourly = last_rate  # Drift updates hourly
            return FundingRate(
                platform=self.platform,
                symbol=symbol.upper(),
                rate_hourly=rate_hourly,
                rate_annualised=rate_hourly * 8760,
                raw={"market_index": market_idx, "last_funding_rate": last_rate},
            )
        except Exception as e:
            log.debug(f"Drift funding rate error for {symbol}: {e}")
            return None

    async def get_all_funding_rates(self) -> List[FundingRate]:
        rates = []
        for sym in self._symbols:
            fr = await self.get_funding_rate(sym)
            if fr:
                rates.append(fr)
        return rates

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        if not self._drift_client:
            return None
        try:
            market_idx = self._market_map.get(symbol.upper())
            if market_idx is None:
                return None

            from driftpy.constants.numeric_constants import PRICE_PRECISION

            oracle_data = self._drift_client.get_oracle_price_data_for_perp_market(market_idx)
            return oracle_data.price / PRICE_PRECISION
        except Exception as e:
            log.debug(f"Drift mark price error for {symbol}: {e}")
            return None

    async def get_balance(self) -> AccountBalance:
        if not self._drift_user:
            return AccountBalance(
                platform=self.platform, equity_usd=0,
                free_margin_usd=0, used_margin_usd=0, unrealised_pnl_usd=0,
            )
        try:
            from driftpy.constants.numeric_constants import QUOTE_PRECISION

            total_collateral = self._drift_user.get_total_collateral() / QUOTE_PRECISION
            free_collateral = self._drift_user.get_free_collateral() / QUOTE_PRECISION
            unrealized_pnl = self._drift_user.get_unrealized_pnl(with_funding=True) / QUOTE_PRECISION

            return AccountBalance(
                platform=self.platform,
                equity_usd=total_collateral,
                free_margin_usd=free_collateral,
                used_margin_usd=total_collateral - free_collateral,
                unrealised_pnl_usd=unrealized_pnl,
            )
        except Exception as e:
            log.error(f"Drift balance error: {e}")
            return AccountBalance(
                platform=self.platform, equity_usd=0,
                free_margin_usd=0, used_margin_usd=0, unrealised_pnl_usd=0,
            )

    async def get_open_positions(self) -> List[Dict]:
        if not self._drift_user:
            return []
        try:
            positions = self._drift_user.get_active_perp_positions()
            return [
                {
                    "market_index": p.market_index,
                    "base_asset_amount": p.base_asset_amount,
                    "quote_asset_amount": p.quote_asset_amount,
                    "symbol": next(
                        (s for s, idx in self._market_map.items() if idx == p.market_index),
                        f"MARKET_{p.market_index}",
                    ),
                }
                for p in positions
            ]
        except Exception as e:
            log.error(f"Drift positions error: {e}")
            return []

    async def open_position(
        self, symbol: str, side: Side, size_usd: float,
        leverage: float = 1.0, max_slippage_pct: float = 0.5,
    ) -> TradeResult:
        if not self._drift_client:
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=side, size=0, price=0, fee_usd=0,
                error="Drift client not initialised",
            )

        market_idx = self._market_map.get(symbol.upper())
        if market_idx is None:
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=side, size=0, price=0, fee_usd=0,
                error=f"Symbol {symbol} not found on Drift",
            )

        mark_price = await self.get_mark_price(symbol)
        if not mark_price:
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=side, size=0, price=0, fee_usd=0,
                error="Could not fetch mark price",
            )

        from driftpy.constants.numeric_constants import BASE_PRECISION
        size_base = size_usd / mark_price

        if settings.dry_run:
            log.info(f"[DRY RUN] Drift {side.value} {size_base:.6f} {symbol} @ ~${mark_price:.2f}")
            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=side, size=size_base, price=mark_price,
                fee_usd=size_usd * 0.001, order_id="dry_run",
            )

        try:
            from driftpy.types import PositionDirection

            direction = PositionDirection.Long() if side == Side.LONG else PositionDirection.Short()
            base_amount = int(size_base * BASE_PRECISION)

            tx_sig = await self._drift_client.open_position(
                direction,
                base_amount,
                market_idx,
            )

            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=side, size=size_base, price=mark_price,
                fee_usd=size_usd * 0.001,
                order_id=str(tx_sig),
            )
        except Exception as e:
            log.error(f"Drift trade error: {e}")
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=side, size=0, price=0, fee_usd=0, error=str(e),
            )

    async def close_position(
        self, symbol: str, side: Side, size: Optional[float] = None,
    ) -> TradeResult:
        opposite = Side.SHORT if side == Side.LONG else Side.LONG

        if not self._drift_client:
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=opposite, size=0, price=0, fee_usd=0,
                error="Drift client not initialised",
            )

        market_idx = self._market_map.get(symbol.upper())
        mark_price = await self.get_mark_price(symbol) or 0

        if settings.dry_run:
            log.info(f"[DRY RUN] Drift close {side.value} {symbol}")
            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=opposite, size=size or 0, price=mark_price,
                fee_usd=(size or 0) * mark_price * 0.001,
                order_id="dry_run_close",
            )

        try:
            tx_sig = await self._drift_client.close_position(market_idx)
            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=opposite, size=size or 0, price=mark_price,
                fee_usd=(size or 0) * mark_price * 0.001,
                order_id=str(tx_sig),
            )
        except Exception as e:
            log.error(f"Drift close error: {e}")
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=opposite, size=0, price=0, fee_usd=0, error=str(e),
            )

    async def estimate_fees(self, symbol: str, size_usd: float) -> float:
        # Drift: taker ~10 bps, round-trip ~20 bps
        return size_usd * 0.002
