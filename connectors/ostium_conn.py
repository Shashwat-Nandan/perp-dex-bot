"""
Ostium perpetual DEX connector.
Uses ostium-python-sdk on Arbitrum + REST/GraphQL fallback.
Specialises in RWAs (stocks, indices, forex) but also has crypto perps.

Price data: https://metadata-backend.ostium.io
Pair data / funding: GraphQL subgraph or ostium-python-sdk
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

OSTIUM_PRICE_BASE = "https://metadata-backend.ostium.io"
OSTIUM_SUBGRAPH_URL = (
    "https://api.subgraph.ormilabs.com/api/public/"
    "67a599d5-c8d2-4cc4-9c4d-2975a97bc5d8/subgraphs/ost-prod/live/gn"
)

# Arbitrum produces ~1 block per 0.25s → ~126,144,000 blocks/year
# But funding rate is already per-block; we convert to hourly then annualise.
_ARB_BLOCKS_PER_HOUR = 14_400  # ~0.25s blocks


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
        self._pair_data: Dict[str, dict] = {}  # symbol -> full pair data

    async def initialise(self) -> None:
        self._session = aiohttp.ClientSession()
        try:
            from ostium_python_sdk import OstiumSDK
            self._sdk = OstiumSDK(
                network="mainnet",
                private_key=self._private_key,
                rpc_url=self._rpc_url,
            )
            log.info("Ostium SDK initialised")
        except ImportError:
            log.warning("ostium-python-sdk not installed; using REST/subgraph fallback")

        await self._load_markets()
        log.info(f"Ostium initialised – {len(self._symbols)} markets")

    async def shutdown(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, url: str, params: dict = None) -> dict:
        async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _graphql(self, query: str, variables: dict = None) -> dict:
        """Execute a GraphQL query against the Ostium subgraph."""
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        async with self._session.post(
            OSTIUM_SUBGRAPH_URL,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _load_markets(self):
        """Load pairs from subgraph (or SDK)."""
        try:
            if self._sdk:
                try:
                    pairs = self._sdk.subgraph.get_pairs()
                    if pairs:
                        for p in pairs:
                            sym = self.normalise_symbol(
                                p.get("from", p.get("symbol", ""))
                            )
                            idx = int(p.get("pairIndex", p.get("index", 0)))
                            if sym:
                                self._pair_map[sym] = idx
                                self._pair_data[sym] = p
                                self._symbols.append(sym)
                        return
                except Exception as e:
                    log.warning(f"Ostium SDK get_pairs failed, falling back to subgraph: {e}")

            # Subgraph fallback
            query = """
            {
                pairs(first: 200) {
                    pairIndex
                    from
                    to
                    lastFundingRate
                    maxFundingFeePerBlock
                    accFundingLong
                    accFundingShort
                    longOI
                    shortOI
                    maxOI
                }
            }
            """
            result = await self._graphql(query)
            pairs = result.get("data", {}).get("pairs", [])
            for p in pairs:
                sym = self.normalise_symbol(p.get("from", ""))
                idx = int(p.get("pairIndex", 0))
                if sym:
                    self._pair_map[sym] = idx
                    self._pair_data[sym] = p
                    self._symbols.append(sym)

            if not self._symbols:
                log.warning("Ostium subgraph returned no pairs")
        except Exception as e:
            log.warning(f"Could not load Ostium markets: {e}")

    async def get_available_symbols(self) -> List[str]:
        return self._symbols

    async def get_funding_rate(self, symbol: str) -> Optional[FundingRate]:
        """Get funding rate for a single symbol from cached pair data."""
        pair = self._pair_data.get(symbol.upper())
        if not pair:
            return None
        try:
            # lastFundingRate is per-block, scaled by 1e18
            raw_rate = pair.get("lastFundingRate", "0")
            per_block_rate = float(raw_rate) / 1e18 if abs(float(raw_rate)) > 1 else float(raw_rate)
            rate_hourly = per_block_rate * _ARB_BLOCKS_PER_HOUR
            return FundingRate(
                platform=self.platform,
                symbol=symbol.upper(),
                rate_hourly=rate_hourly,
                rate_annualised=rate_hourly * 8760,
                raw=pair,
            )
        except Exception as e:
            log.debug(f"Ostium funding rate error for {symbol}: {e}")
            return None

    async def get_all_funding_rates(self) -> List[FundingRate]:
        """Fetch fresh pair data from subgraph and compute funding rates."""
        # Refresh pair data to get latest funding rates
        try:
            if self._sdk:
                try:
                    pairs = self._sdk.subgraph.get_pairs()
                    if pairs:
                        for p in pairs:
                            sym = self.normalise_symbol(
                                p.get("from", p.get("symbol", ""))
                            )
                            if sym:
                                self._pair_data[sym] = p
                except Exception as e:
                    log.debug(f"Ostium SDK refresh failed: {e}")

            if not self._sdk or not self._pair_data:
                query = """
                {
                    pairs(first: 200) {
                        pairIndex
                        from
                        to
                        lastFundingRate
                        maxFundingFeePerBlock
                        longOI
                        shortOI
                    }
                }
                """
                result = await self._graphql(query)
                pairs = result.get("data", {}).get("pairs", [])
                for p in pairs:
                    sym = self.normalise_symbol(p.get("from", ""))
                    if sym:
                        self._pair_data[sym] = p
                        if sym not in self._pair_map:
                            self._pair_map[sym] = int(p.get("pairIndex", 0))
                            self._symbols.append(sym)
        except Exception as e:
            log.warning(f"Ostium funding rate refresh failed: {e}")

        # Build FundingRate objects from pair data
        rates = []
        for sym in self._symbols:
            fr = await self.get_funding_rate(sym)
            if fr:
                rates.append(fr)

        if not rates:
            log.warning(f"Ostium: returned 0 funding rates (tried {len(self._symbols)} pairs)")
        return rates

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        """Fetch price from Ostium metadata backend."""
        try:
            data = await self._get(
                f"{OSTIUM_PRICE_BASE}/PricePublish/latest-price",
                {"asset": f"{symbol.upper()}USD"},
            )
            # Response: {"feed_id": ..., "mid": "66000.5", ...}
            if isinstance(data, dict):
                mid = data.get("mid")
                if mid:
                    return float(mid)
            # Might be nested in an array
            if isinstance(data, list) and data:
                return float(data[0].get("mid", 0))
        except Exception as e:
            log.debug(f"Ostium price error for {symbol}: {e}")

        # Fallback: try the batch endpoint and filter
        try:
            data = await self._get(f"{OSTIUM_PRICE_BASE}/PricePublish/latest-prices")
            if isinstance(data, list):
                target = f"{symbol.upper()}USD"
                for item in data:
                    asset = item.get("from", "") + item.get("to", "")
                    if asset == target or item.get("feed_id", "").upper().startswith(symbol.upper()):
                        return float(item.get("mid", 0))
        except Exception as e:
            log.debug(f"Ostium batch price error for {symbol}: {e}")
        return None

    async def get_balance(self) -> AccountBalance:
        try:
            if self._sdk:
                balance_data = self._sdk.get_balance()
                return AccountBalance(
                    platform=self.platform,
                    equity_usd=float(balance_data.get("equity", 0)),
                    free_margin_usd=float(balance_data.get("freeMargin", 0)),
                    used_margin_usd=float(balance_data.get("usedMargin", 0)),
                    unrealised_pnl_usd=float(balance_data.get("unrealizedPnl", 0)),
                )
            log.warning("Ostium: SDK not available for balance check")
            return AccountBalance(
                platform=self.platform, equity_usd=0,
                free_margin_usd=0, used_margin_usd=0, unrealised_pnl_usd=0,
            )
        except Exception as e:
            log.error(f"Ostium balance error: {e}")
            return AccountBalance(
                platform=self.platform, equity_usd=0,
                free_margin_usd=0, used_margin_usd=0, unrealised_pnl_usd=0,
            )

    async def get_open_positions(self) -> List[Dict]:
        try:
            if self._sdk:
                positions = self._sdk.get_positions()
                return positions if isinstance(positions, list) else []
            return []
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
