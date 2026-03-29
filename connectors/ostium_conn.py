"""
Ostium perpetual DEX connector.
Uses ostium-python-sdk on Arbitrum.
Specialises in RWAs (stocks, indices, forex) but also has crypto perps.
"""

import asyncio
from typing import Dict, List, Optional

import aiohttp

from config import settings
from utils.logger import get_logger
from utils.models import (
    AccountBalance, FundingRate, Platform, Side, TradeResult,
)
from .base import BaseConnector

log = get_logger("connector.ostium")

OSTIUM_API_BASE = "https://api.ostium.io"


class OstiumConnector(BaseConnector):
    platform = Platform.OSTIUM

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._address = settings.wallet.evm_public_key
        self._private_key = settings.wallet.evm_private_key
        self._rpc_url = settings.rpc.arbitrum
        self._sdk = None
        self._symbols: List[str] = []
        self._pair_map: Dict[str, int] = {}  # symbol -> pairIndex

    async def initialise(self) -> None:
        self._session = aiohttp.ClientSession()
        try:
            from ostium_python_sdk import OstiumSDK
            self._sdk = OstiumSDK(
                private_key=self._private_key,
                rpc_url=self._rpc_url,
            )
            log.info("Ostium SDK initialised")
        except ImportError:
            log.warning("ostium-python-sdk not installed; using REST fallback")

        await self._load_markets()
        log.info(f"Ostium initialised – {len(self._symbols)} markets")

    async def shutdown(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, params: dict = None) -> dict:
        url = f"{OSTIUM_API_BASE}{path}"
        async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _load_markets(self):
        try:
            data = await self._get("/v1/pairs")
            pairs = data if isinstance(data, list) else data.get("pairs", [])
            for p in pairs:
                sym = self.normalise_symbol(p.get("symbol", p.get("from", "")))
                idx = p.get("pairIndex", p.get("index", 0))
                if sym:
                    self._pair_map[sym] = idx
                    self._symbols.append(sym)
        except Exception as e:
            log.warning(f"Could not load Ostium markets: {e}")
            self._symbols = [
                "BTC", "ETH", "SOL", "BNB", "ARB", "DOGE", "AVAX", "LINK",
                "OP", "SUI", "APT", "INJ", "SEI", "TIA", "NEAR", "FTM",
                "MATIC", "ATOM", "DOT", "ADA",
            ]

    async def get_available_symbols(self) -> List[str]:
        return self._symbols

    async def get_funding_rate(self, symbol: str) -> Optional[FundingRate]:
        try:
            pair_idx = self._pair_map.get(symbol.upper())
            if pair_idx is None:
                return None
            data = await self._get(f"/v1/funding_rate/{pair_idx}")
            rate_hourly = float(data.get("fundingRate", data.get("rollingFee", 0)))
            return FundingRate(
                platform=self.platform,
                symbol=symbol.upper(),
                rate_hourly=rate_hourly,
                rate_annualised=rate_hourly * 8760,
                raw=data,
            )
        except Exception as e:
            log.debug(f"Ostium funding rate error for {symbol}: {e}")
            return None

    async def get_all_funding_rates(self) -> List[FundingRate]:
        # Try batch endpoint first
        try:
            data = await self._get("/v1/funding_rates")
            items = data if isinstance(data, list) else data.get("fundingRates", data.get("data", []))
            if isinstance(items, list) and items:
                rates = []
                for item in items:
                    sym = self.normalise_symbol(item.get("symbol", item.get("from", "")))
                    if not sym:
                        continue
                    rate_hourly = float(item.get("fundingRate", item.get("rollingFee", 0)))
                    rates.append(FundingRate(
                        platform=self.platform,
                        symbol=sym,
                        rate_hourly=rate_hourly,
                        rate_annualised=rate_hourly * 8760,
                        raw=item,
                    ))
                if rates:
                    return rates
                log.warning("Ostium batch endpoint returned data but parsed 0 rates")
            else:
                log.warning(f"Ostium batch endpoint returned unexpected format: {type(items)}")
        except Exception as e:
            log.warning(f"Ostium batch funding rates endpoint failed: {e}")

        # Fallback: fetch per-symbol concurrently
        symbols = self._symbols or [
            "BTC", "ETH", "SOL", "BNB", "ARB", "DOGE", "AVAX", "LINK",
            "OP", "SUI", "APT", "INJ", "SEI", "TIA", "NEAR", "FTM",
            "MATIC", "ATOM", "DOT", "ADA",
        ]
        tasks = [self.get_funding_rate(sym) for sym in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        rates = []
        for r in results:
            if isinstance(r, FundingRate):
                rates.append(r)
            elif isinstance(r, Exception):
                log.debug(f"Ostium funding rate fetch error: {r}")
        if not rates:
            log.warning(f"Ostium: per-symbol fallback also returned 0 rates (tried {len(symbols)} symbols)")
        return rates

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        try:
            pair_idx = self._pair_map.get(symbol.upper())
            if pair_idx is None:
                return None
            data = await self._get(f"/v1/price/{pair_idx}")
            return float(data.get("price", data.get("markPrice", 0)))
        except Exception as e:
            log.debug(f"Ostium price error for {symbol}: {e}")
            return None

    async def get_balance(self) -> AccountBalance:
        try:
            if self._sdk:
                # Use SDK method
                balance_data = self._sdk.get_balance()
                return AccountBalance(
                    platform=self.platform,
                    equity_usd=float(balance_data.get("equity", 0)),
                    free_margin_usd=float(balance_data.get("freeMargin", 0)),
                    used_margin_usd=float(balance_data.get("usedMargin", 0)),
                    unrealised_pnl_usd=float(balance_data.get("unrealizedPnl", 0)),
                )
            data = await self._get(f"/v1/account/{self._address}")
            return AccountBalance(
                platform=self.platform,
                equity_usd=float(data.get("equity", 0)),
                free_margin_usd=float(data.get("freeMargin", 0)),
                used_margin_usd=float(data.get("usedMargin", 0)),
                unrealised_pnl_usd=float(data.get("unrealizedPnl", 0)),
            )
        except Exception as e:
            log.error(f"Ostium balance error: {e}")
            return AccountBalance(
                platform=self.platform, equity_usd=0,
                free_margin_usd=0, used_margin_usd=0, unrealised_pnl_usd=0,
            )

    async def get_open_positions(self) -> List[Dict]:
        try:
            data = await self._get(f"/v1/positions/{self._address}")
            return data if isinstance(data, list) else data.get("positions", [])
        except Exception as e:
            log.error(f"Ostium positions error: {e}")
            return []

    async def open_position(
        self, symbol: str, side: Side, size_usd: float,
        leverage: float = 1.0, max_slippage_pct: float = 0.5,
    ) -> TradeResult:
        mark_price = await self.get_mark_price(symbol)
        if not mark_price:
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=side, size=0, price=0, fee_usd=0,
                error="Could not fetch mark price",
            )

        size_base = size_usd / mark_price
        pair_idx = self._pair_map.get(symbol.upper())

        if settings.dry_run:
            log.info(f"[DRY RUN] Ostium {side.value} {size_base:.6f} {symbol} @ ~${mark_price:.2f}")
            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=side, size=size_base, price=mark_price,
                fee_usd=size_usd * 0.001, order_id="dry_run",
            )

        try:
            if self._sdk and pair_idx is not None:
                is_long = side == Side.LONG
                result = self._sdk.open_trade(
                    pair_index=pair_idx,
                    is_long=is_long,
                    collateral=size_usd / leverage,
                    leverage=int(leverage),
                    slippage=max_slippage_pct,
                )
                return TradeResult(
                    success=True, platform=self.platform, symbol=symbol,
                    side=side, size=size_base, price=mark_price,
                    fee_usd=size_usd * 0.001,
                    order_id=str(result.get("tradeId", "")),
                    raw=result,
                )
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=side, size=0, price=0, fee_usd=0,
                error="SDK not available or pair not found",
            )
        except Exception as e:
            log.error(f"Ostium trade error: {e}")
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=side, size=0, price=0, fee_usd=0, error=str(e),
            )

    async def close_position(
        self, symbol: str, side: Side, size: Optional[float] = None,
    ) -> TradeResult:
        opposite = Side.SHORT if side == Side.LONG else Side.LONG
        mark_price = await self.get_mark_price(symbol) or 0

        if settings.dry_run:
            log.info(f"[DRY RUN] Ostium close {side.value} {symbol}")
            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=opposite, size=size or 0, price=mark_price,
                fee_usd=(size or 0) * mark_price * 0.001,
                order_id="dry_run_close",
            )

        try:
            if self._sdk:
                pair_idx = self._pair_map.get(symbol.upper())
                positions = await self.get_open_positions()
                for p in positions:
                    if p.get("pairIndex") == pair_idx:
                        trade_idx = p.get("index", p.get("tradeIndex"))
                        result = self._sdk.close_trade(trade_index=trade_idx)
                        return TradeResult(
                            success=True, platform=self.platform, symbol=symbol,
                            side=opposite, size=size or 0, price=mark_price,
                            fee_usd=(size or 0) * mark_price * 0.001,
                            raw=result,
                        )
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=opposite, size=0, price=0, fee_usd=0,
                error="Could not close position",
            )
        except Exception as e:
            log.error(f"Ostium close error: {e}")
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=opposite, size=0, price=0, fee_usd=0, error=str(e),
            )

    async def estimate_fees(self, symbol: str, size_usd: float) -> float:
        # Ostium: ~10 bps opening + rolling fees; round-trip ~20 bps
        return size_usd * 0.002
