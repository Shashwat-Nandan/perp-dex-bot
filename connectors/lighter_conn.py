"""
Lighter DEX connector.
Uses lighter REST API on Ethereum L2 (custom zk-rollup).
API docs: https://apidocs.lighter.xyz
"""

import asyncio
import time
from typing import Dict, List, Optional

import aiohttp

from config import settings
from utils.logger import get_logger
from utils.models import (
    AccountBalance, FundingRate, Platform, Side, TradeResult,
)
from .base import BaseConnector

log = get_logger("connector.lighter")

LIGHTER_API_BASE = "https://mainnet.zklighter.elliot.ai"


class LighterConnector(BaseConnector):
    platform = Platform.LIGHTER

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._address = settings.wallet.evm_public_key
        self._private_key = settings.wallet.evm_private_key
        self._sdk_client = None
        self._markets: Dict[str, dict] = {}  # symbol -> market info
        self._market_ids: Dict[str, int] = {}  # symbol -> market_id
        self._symbols: List[str] = []

    async def initialise(self) -> None:
        self._session = aiohttp.ClientSession()
        try:
            from lighter.lighter_client import Client as LighterClient
            self._sdk_client = LighterClient(
                private_key=self._private_key,
                api_auth=self._address,
            )
            log.info("Lighter SDK client initialised")
        except ImportError:
            log.warning("lighter-sdk not installed; using REST API fallback")

        await self._load_markets()
        log.info(f"Lighter initialised – {len(self._symbols)} markets")

    async def shutdown(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, params: dict = None) -> dict:
        url = f"{LIGHTER_API_BASE}{path}"
        async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _load_markets(self):
        """Load perp markets from orderBooks endpoint."""
        try:
            data = await self._get("/api/v1/orderBooks", {"filter": "perp"})
            order_books = data.get("order_books", []) if isinstance(data, dict) else []
            for m in order_books:
                sym = self.normalise_symbol(m.get("symbol", m.get("name", "")))
                market_id = m.get("market_id")
                if sym and market_id is not None:
                    self._markets[sym] = m
                    self._market_ids[sym] = int(market_id)
                    self._symbols.append(sym)

            if not self._symbols:
                log.warning(
                    "Lighter orderBooks returned no perp markets — "
                    f"raw keys: {list(data.keys()) if isinstance(data, dict) else type(data)}"
                )
        except Exception as e:
            log.warning(f"Could not load Lighter markets: {e}")

    async def get_available_symbols(self) -> List[str]:
        return self._symbols

    async def get_funding_rate(self, symbol: str) -> Optional[FundingRate]:
        # Lighter has no single-symbol endpoint; this is only used as a
        # convenience wrapper — callers should prefer get_all_funding_rates.
        rates = await self.get_all_funding_rates()
        for r in rates:
            if r.symbol == symbol.upper():
                return r
        return None

    async def get_all_funding_rates(self) -> List[FundingRate]:
        """Fetch funding rates from /api/v1/funding-rates (hyphen, not underscore).

        The response includes rates from multiple exchanges; we filter
        for exchange == "lighter" only.
        """
        try:
            data = await self._get("/api/v1/funding-rates")
            items = data.get("funding_rates", []) if isinstance(data, dict) else []
            if not isinstance(items, list):
                log.warning(f"Lighter funding-rates returned unexpected format: {type(items)}")
                return []

            rates = []
            for item in items:
                # Only use Lighter's own rates, not cross-exchange data
                if item.get("exchange", "").lower() != "lighter":
                    continue
                sym = self.normalise_symbol(item.get("symbol", ""))
                if not sym:
                    continue
                # Lighter rate is hourly (settles every hour)
                rate_hourly = float(item.get("rate", 0))
                rates.append(FundingRate(
                    platform=self.platform,
                    symbol=sym,
                    rate_hourly=rate_hourly,
                    rate_annualised=rate_hourly * 8760,
                    raw=item,
                ))

            if not rates:
                log.warning(
                    f"Lighter: parsed 0 rates with exchange=lighter "
                    f"(total items in response: {len(items)})"
                )
            return rates
        except Exception as e:
            log.warning(f"Lighter funding-rates endpoint failed: {e}")
            return []

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        """Fetch mark/last price from orderBookDetails endpoint."""
        try:
            data = await self._get("/api/v1/orderBookDetails")
            details = data.get("order_book_details", data.get("data", []))
            if isinstance(details, list):
                for entry in details:
                    entry_sym = self.normalise_symbol(entry.get("symbol", ""))
                    if entry_sym == symbol.upper():
                        return float(entry.get("last_trade_price", 0))
            elif isinstance(details, dict):
                # Could be keyed by market_id or symbol
                for _key, entry in details.items():
                    entry_sym = self.normalise_symbol(entry.get("symbol", ""))
                    if entry_sym == symbol.upper():
                        return float(entry.get("last_trade_price", 0))
        except Exception as e:
            log.debug(f"Lighter mark price failed for {symbol}: {e}")
        return None

    async def get_balance(self) -> AccountBalance:
        try:
            data = await self._get(f"/api/v1/account", {"address": self._address})
            return AccountBalance(
                platform=self.platform,
                equity_usd=float(data.get("equity", 0)),
                free_margin_usd=float(data.get("freeMargin", data.get("availableBalance", 0))),
                used_margin_usd=float(data.get("usedMargin", 0)),
                unrealised_pnl_usd=float(data.get("unrealizedPnl", 0)),
            )
        except Exception as e:
            log.error(f"Lighter balance fetch error: {e}")
            return AccountBalance(
                platform=self.platform, equity_usd=0,
                free_margin_usd=0, used_margin_usd=0, unrealised_pnl_usd=0,
            )

    async def get_open_positions(self) -> List[Dict]:
        try:
            data = await self._get(f"/api/v1/positions", {"address": self._address})
            return data if isinstance(data, list) else data.get("positions", [])
        except Exception as e:
            log.error(f"Lighter positions fetch error: {e}")
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

        if settings.dry_run:
            log.info(f"[DRY RUN] Lighter {side.value} {size_base:.6f} {symbol} @ ~${mark_price:.2f}")
            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=side, size=size_base, price=mark_price,
                fee_usd=size_usd * 0.0005, order_id="dry_run",
            )

        try:
            if self._sdk_client:
                is_buy = side == Side.LONG
                result = self._sdk_client.create_order(
                    symbol=f"{symbol}USD",
                    side="buy" if is_buy else "sell",
                    size=size_base,
                    order_type="market",
                )
                return TradeResult(
                    success=True, platform=self.platform, symbol=symbol,
                    side=side, size=size_base, price=mark_price,
                    fee_usd=size_usd * 0.0005,
                    order_id=str(result.get("orderId", "")),
                    raw=result,
                )
            else:
                return TradeResult(
                    success=False, platform=self.platform, symbol=symbol,
                    side=side, size=0, price=0, fee_usd=0,
                    error="Lighter SDK not available for trading",
                )
        except Exception as e:
            log.error(f"Lighter trade error: {e}")
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=side, size=0, price=0, fee_usd=0, error=str(e),
            )

    async def close_position(
        self, symbol: str, side: Side, size: Optional[float] = None,
    ) -> TradeResult:
        opposite = Side.SHORT if side == Side.LONG else Side.LONG

        if size is None:
            positions = await self.get_open_positions()
            for p in positions:
                if self.normalise_symbol(p.get("symbol", "")) == symbol.upper():
                    size = abs(float(p.get("size", 0)))
                    break

        if not size:
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=opposite, size=0, price=0, fee_usd=0,
                error="No position found",
            )

        mark_price = await self.get_mark_price(symbol) or 0
        size_usd = size * mark_price

        if settings.dry_run:
            log.info(f"[DRY RUN] Lighter close {side.value} {size:.6f} {symbol}")
            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=opposite, size=size, price=mark_price,
                fee_usd=size_usd * 0.0005, order_id="dry_run_close",
            )

        try:
            if self._sdk_client:
                is_buy = opposite == Side.LONG
                result = self._sdk_client.create_order(
                    symbol=f"{symbol}USD",
                    side="buy" if is_buy else "sell",
                    size=size,
                    order_type="market",
                    reduce_only=True,
                )
                return TradeResult(
                    success=True, platform=self.platform, symbol=symbol,
                    side=opposite, size=size, price=mark_price,
                    fee_usd=size_usd * 0.0005,
                    raw=result,
                )
            else:
                return TradeResult(
                    success=False, platform=self.platform, symbol=symbol,
                    side=opposite, size=0, price=0, fee_usd=0,
                    error="SDK not available",
                )
        except Exception as e:
            log.error(f"Lighter close error: {e}")
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=opposite, size=0, price=0, fee_usd=0, error=str(e),
            )

    async def estimate_fees(self, symbol: str, size_usd: float) -> float:
        # Lighter: 0 bps maker/taker currently, but budget ~5 bps round-trip
        return size_usd * 0.001
