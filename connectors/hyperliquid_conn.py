"""
Hyperliquid perpetual DEX connector.
Uses the hyperliquid-python-sdk for all operations.
Deposits via Arbitrum; trades on Hyperliquid L1.
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

log = get_logger("connector.hyperliquid")

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
HL_EXCHANGE_URL = "https://api.hyperliquid.xyz/exchange"


class HyperliquidConnector(BaseConnector):
    platform = Platform.HYPERLIQUID

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._address = settings.wallet.evm_public_key
        self._api_key = settings.platform_keys.hl_api_wallet_key
        self._sdk_client = None  # lazy init
        self._meta: Dict = {}
        self._symbols: List[str] = []

    # ── lifecycle ────────────────────────────────────────────────────────

    async def initialise(self) -> None:
        self._session = aiohttp.ClientSession()
        # Attempt SDK import (optional – falls back to raw HTTP)
        try:
            from hyperliquid.info import Info
            from hyperliquid.exchange import Exchange
            from hyperliquid.utils import constants
            info = Info(constants.MAINNET_API_URL, skip_ws=True)
            self._sdk_info = info
            if self._api_key:
                self._sdk_exchange = Exchange(
                    wallet=None,  # we sign manually
                    base_url=constants.MAINNET_API_URL,
                )
            log.info("Hyperliquid SDK loaded")
        except ImportError:
            log.warning("hyperliquid-python-sdk not installed; using raw HTTP")
            self._sdk_info = None
            self._sdk_exchange = None

        # fetch market metadata
        await self._load_meta()
        log.info(f"Hyperliquid initialised – {len(self._symbols)} markets")

    async def shutdown(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── internal helpers ─────────────────────────────────────────────────

    async def _post_info(self, payload: dict) -> dict:
        async with self._session.post(HL_INFO_URL, json=payload) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _load_meta(self):
        data = await self._post_info({"type": "meta"})
        self._meta = data
        universe = data.get("universe", [])
        self._symbols = [m["name"] for m in universe]

    def _hl_symbol(self, symbol: str) -> Optional[str]:
        """Map canonical symbol to HL market name."""
        up = symbol.upper()
        if up in self._symbols:
            return up
        return None

    # ── market data ──────────────────────────────────────────────────────

    async def get_available_symbols(self) -> List[str]:
        return [self.normalise_symbol(s) for s in self._symbols]

    async def get_funding_rate(self, symbol: str) -> Optional[FundingRate]:
        hl_sym = self._hl_symbol(symbol)
        if not hl_sym:
            return None
        try:
            data = await self._post_info({
                "type": "fundingHistory",
                "coin": hl_sym,
                "startTime": int((time.time() - 3600) * 1000),
            })
            if not data:
                return None
            latest = data[-1]
            hourly = float(latest["fundingRate"])
            return FundingRate(
                platform=self.platform,
                symbol=symbol.upper(),
                rate_hourly=hourly,
                rate_annualised=hourly * 8760,
                raw=latest,
            )
        except Exception as e:
            log.error(f"Error fetching HL funding rate for {symbol}: {e}")
            return None

    async def get_all_funding_rates(self) -> List[FundingRate]:
        # Use meta endpoint which includes predicted funding
        try:
            data = await self._post_info({"type": "metaAndAssetCtxs"})
            meta = data[0]["universe"]
            ctxs = data[1]
            rates = []
            for m, ctx in zip(meta, ctxs):
                sym = m["name"]
                funding = float(ctx.get("funding", 0))
                rates.append(FundingRate(
                    platform=self.platform,
                    symbol=self.normalise_symbol(sym),
                    rate_hourly=funding,
                    rate_annualised=funding * 8760,
                    raw=ctx,
                ))
            return rates
        except Exception as e:
            log.error(f"Error fetching all HL funding rates: {e}")
            return []

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        hl_sym = self._hl_symbol(symbol)
        if not hl_sym:
            return None
        try:
            data = await self._post_info({"type": "metaAndAssetCtxs"})
            meta = data[0]["universe"]
            ctxs = data[1]
            for m, ctx in zip(meta, ctxs):
                if m["name"] == hl_sym:
                    return float(ctx.get("markPx", 0))
        except Exception as e:
            log.error(f"Error fetching HL mark price for {symbol}: {e}")
        return None

    # ── account ──────────────────────────────────────────────────────────

    async def get_balance(self) -> AccountBalance:
        data = await self._post_info({
            "type": "clearinghouseState",
            "user": self._address,
        })
        margin = data.get("marginSummary", {})
        account_value = float(margin.get("accountValue", 0))
        total_margin_used = float(margin.get("totalMarginUsed", 0))
        return AccountBalance(
            platform=self.platform,
            equity_usd=account_value,
            free_margin_usd=account_value - total_margin_used,
            used_margin_usd=total_margin_used,
            unrealised_pnl_usd=float(margin.get("totalUnrealizedPnl", 0)),
        )

    async def get_open_positions(self) -> List[Dict]:
        data = await self._post_info({
            "type": "clearinghouseState",
            "user": self._address,
        })
        return data.get("assetPositions", [])

    # ── trading ──────────────────────────────────────────────────────────

    async def open_position(
        self, symbol: str, side: Side, size_usd: float,
        leverage: float = 1.0, max_slippage_pct: float = 0.5,
    ) -> TradeResult:
        hl_sym = self._hl_symbol(symbol)
        if not hl_sym:
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=side, size=0, price=0, fee_usd=0,
                error=f"Symbol {symbol} not available on Hyperliquid",
            )

        mark_price = await self.get_mark_price(symbol)
        if not mark_price:
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=side, size=0, price=0, fee_usd=0,
                error="Could not fetch mark price",
            )

        size_base = size_usd / mark_price
        is_buy = side == Side.LONG

        if settings.dry_run:
            log.info(f"[DRY RUN] HL {side.value} {size_base:.6f} {symbol} @ ~${mark_price:.2f}")
            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=side, size=size_base, price=mark_price,
                fee_usd=size_usd * 0.00035,  # ~3.5 bps taker fee
                order_id="dry_run",
            )

        # Real trade via SDK
        try:
            from hyperliquid.exchange import Exchange
            from hyperliquid.utils import constants
            from eth_account import Account

            wallet = Account.from_key(self._api_key or settings.wallet.evm_private_key)
            exchange = Exchange(wallet, constants.MAINNET_API_URL)

            # Set leverage
            exchange.update_leverage(leverage, hl_sym)

            # Place market order
            slippage = max_slippage_pct / 100
            limit_px = mark_price * (1 + slippage) if is_buy else mark_price * (1 - slippage)

            result = exchange.order(
                hl_sym, is_buy, size_base, limit_px,
                {"limit": {"tif": "Ioc"}},
            )

            resp_data = result.get("response", {}).get("data", {})
            statuses = resp_data.get("statuses", [])
            if statuses and isinstance(statuses[0], dict) and "filled" in statuses[0]:
                fill_info = statuses[0]["filled"]
                filled_price = float(fill_info.get("avgPx", mark_price))
                return TradeResult(
                    success=True, platform=self.platform, symbol=symbol,
                    side=side, size=size_base, price=filled_price,
                    fee_usd=size_usd * 0.00035,
                    order_id=str(fill_info.get("oid", "")),
                    raw=result,
                )
            else:
                error_detail = str(statuses[0]) if statuses else str(result)
                return TradeResult(
                    success=False, platform=self.platform, symbol=symbol,
                    side=side, size=size_base, price=mark_price, fee_usd=0,
                    error=f"Order not filled: {error_detail}",
                    raw=result,
                )
        except Exception as e:
            log.error(f"HL trade error: {e}")
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=side, size=0, price=0, fee_usd=0, error=str(e),
            )

    async def close_position(
        self, symbol: str, side: Side, size: Optional[float] = None,
    ) -> TradeResult:
        # Closing = opening opposite side with reduce-only
        opposite = Side.SHORT if side == Side.LONG else Side.LONG

        if size is None:
            # get full position size
            positions = await self.get_open_positions()
            hl_sym = self._hl_symbol(symbol)
            for pos in positions:
                p = pos.get("position", {})
                if p.get("coin") == hl_sym:
                    size = abs(float(p.get("szi", 0)))
                    break
            if size is None or size == 0:
                return TradeResult(
                    success=False, platform=self.platform, symbol=symbol,
                    side=side, size=0, price=0, fee_usd=0,
                    error="No open position found to close",
                )

        mark_price = await self.get_mark_price(symbol) or 0
        size_usd = size * mark_price

        if settings.dry_run:
            log.info(f"[DRY RUN] HL close {side.value} {size:.6f} {symbol}")
            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=opposite, size=size, price=mark_price,
                fee_usd=size_usd * 0.00035, order_id="dry_run_close",
            )

        # Real close via SDK (market IOC reduce-only)
        try:
            from hyperliquid.exchange import Exchange
            from hyperliquid.utils import constants
            from eth_account import Account

            wallet = Account.from_key(self._api_key or settings.wallet.evm_private_key)
            exchange = Exchange(wallet, constants.MAINNET_API_URL)
            is_buy = opposite == Side.LONG
            slippage = settings.arb.max_slippage_pct / 100
            limit_px = mark_price * (1 + slippage) if is_buy else mark_price * (1 - slippage)

            result = exchange.order(
                self._hl_symbol(symbol), is_buy, size, limit_px,
                {"limit": {"tif": "Ioc"}}, reduce_only=True,
            )
            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=opposite, size=size, price=mark_price,
                fee_usd=size_usd * 0.00035,
                order_id=str(result),
                raw=result,
            )
        except Exception as e:
            log.error(f"HL close error: {e}")
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=opposite, size=size, price=mark_price, fee_usd=0,
                error=str(e),
            )

    async def estimate_fees(self, symbol: str, size_usd: float) -> float:
        # Hyperliquid: taker 3.5 bps, maker 1 bp; using taker (market orders)
        # Round trip = 2 * taker
        return size_usd * 0.00035 * 2
