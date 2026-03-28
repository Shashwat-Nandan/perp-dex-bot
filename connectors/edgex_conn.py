"""
EdgeX perpetual DEX connector.
REST + WebSocket API on StarkEx (Ethereum L2).
"""

import asyncio
import hashlib
import hmac
import time
from typing import Dict, List, Optional

import aiohttp

from config import settings
from utils.logger import get_logger
from utils.models import (
    AccountBalance, FundingRate, Platform, Side, TradeResult,
)
from .base import BaseConnector

log = get_logger("connector.edgex")

EDGEX_API_BASE = "https://api.edgex.exchange"
EDGEX_WS_BASE = "wss://quote.edgex.exchange"


class EdgeXConnector(BaseConnector):
    platform = Platform.EDGEX

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._api_key = settings.platform_keys.edgex_api_key
        self._api_secret = settings.platform_keys.edgex_api_secret
        self._stark_key = settings.platform_keys.edgex_stark_key
        self._address = settings.wallet.evm_public_key
        self._symbols: List[str] = []
        self._contracts: Dict[str, dict] = {}

    async def initialise(self) -> None:
        self._session = aiohttp.ClientSession()
        await self._load_contracts()
        log.info(f"EdgeX initialised – {len(self._symbols)} markets")

    async def shutdown(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        if not self._api_secret:
            return ""
        message = f"{timestamp}{method.upper()}{path}{body}"
        return hmac.new(
            self._api_secret.encode(), message.encode(), hashlib.sha256
        ).hexdigest()

    def _auth_headers(self, method: str, path: str, body: str = "") -> dict:
        ts = str(int(time.time() * 1000))
        return {
            "Content-Type": "application/json",
            "EDGEX-API-KEY": self._api_key or "",
            "EDGEX-TIMESTAMP": ts,
            "EDGEX-SIGNATURE": self._sign(ts, method, path, body),
        }

    async def _get(self, path: str, params: dict = None, auth: bool = False) -> dict:
        url = f"{EDGEX_API_BASE}{path}"
        headers = self._auth_headers("GET", path) if auth else {"Content-Type": "application/json"}
        async with self._session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _post(self, path: str, data: dict = None, auth: bool = True) -> dict:
        import json as _json
        url = f"{EDGEX_API_BASE}{path}"
        body = _json.dumps(data or {})
        headers = self._auth_headers("POST", path, body) if auth else {"Content-Type": "application/json"}
        async with self._session.post(url, data=body, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _load_contracts(self):
        try:
            data = await self._get("/api/v1/public/contracts")
            contracts = data.get("data", data.get("contracts", data)) if isinstance(data, dict) else data
            if isinstance(contracts, list):
                for c in contracts:
                    sym = self.normalise_symbol(c.get("symbol", c.get("contractName", "")))
                    if sym:
                        self._contracts[sym] = c
                        self._symbols.append(sym)
            elif isinstance(contracts, dict):
                for key, c in contracts.items():
                    sym = self.normalise_symbol(key)
                    if sym:
                        self._contracts[sym] = c
                        self._symbols.append(sym)
        except Exception as e:
            log.warning(f"EdgeX contracts load error: {e}")
            self._symbols = ["BTC", "ETH", "SOL"]

    def _edgex_symbol(self, symbol: str) -> str:
        return f"{symbol.upper()}USD"

    async def get_available_symbols(self) -> List[str]:
        return self._symbols

    async def get_funding_rate(self, symbol: str) -> Optional[FundingRate]:
        try:
            data = await self._get("/api/v1/public/funding_rate", {
                "symbol": self._edgex_symbol(symbol)
            })
            rate_data = data.get("data", data)
            rate = float(rate_data.get("fundingRate", rate_data.get("rate", 0)))
            # EdgeX uses 8-hour funding
            rate_hourly = rate / 8
            return FundingRate(
                platform=self.platform,
                symbol=symbol.upper(),
                rate_hourly=rate_hourly,
                rate_annualised=rate_hourly * 8760,
                raw=rate_data,
            )
        except Exception as e:
            log.debug(f"EdgeX funding rate error for {symbol}: {e}")
            return None

    async def get_all_funding_rates(self) -> List[FundingRate]:
        # Try batch endpoint first (no symbol param = all symbols)
        try:
            data = await self._get("/api/v1/public/funding_rate")
            items = data.get("data", data) if isinstance(data, dict) else data
            if isinstance(items, list) and items:
                rates = []
                for item in items:
                    sym = self.normalise_symbol(item.get("symbol", ""))
                    if not sym:
                        continue
                    rate = float(item.get("fundingRate", item.get("rate", 0)))
                    # EdgeX uses 8-hour funding
                    rate_hourly = rate / 8
                    rates.append(FundingRate(
                        platform=self.platform,
                        symbol=sym,
                        rate_hourly=rate_hourly,
                        rate_annualised=rate_hourly * 8760,
                        raw=item,
                    ))
                if rates:
                    return rates
        except Exception as e:
            log.debug(f"EdgeX batch funding rates endpoint failed: {e}")

        # Fallback: fetch per-symbol concurrently
        tasks = [self.get_funding_rate(sym) for sym in self._symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        rates = []
        for r in results:
            if isinstance(r, FundingRate):
                rates.append(r)
            elif isinstance(r, Exception):
                log.debug(f"EdgeX funding rate fetch error: {r}")
        return rates

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        try:
            data = await self._get("/api/v1/public/ticker", {
                "symbol": self._edgex_symbol(symbol)
            })
            ticker = data.get("data", data)
            return float(ticker.get("markPrice", ticker.get("lastPrice", 0)))
        except Exception as e:
            log.debug(f"EdgeX mark price error for {symbol}: {e}")
            return None

    async def get_balance(self) -> AccountBalance:
        try:
            data = await self._get("/api/v1/private/account", auth=True)
            acct = data.get("data", data)
            return AccountBalance(
                platform=self.platform,
                equity_usd=float(acct.get("equity", 0)),
                free_margin_usd=float(acct.get("availableBalance", 0)),
                used_margin_usd=float(acct.get("usedMargin", 0)),
                unrealised_pnl_usd=float(acct.get("unrealizedPnl", 0)),
            )
        except Exception as e:
            log.error(f"EdgeX balance error: {e}")
            return AccountBalance(
                platform=self.platform, equity_usd=0,
                free_margin_usd=0, used_margin_usd=0, unrealised_pnl_usd=0,
            )

    async def get_open_positions(self) -> List[Dict]:
        try:
            data = await self._get("/api/v1/private/positions", auth=True)
            positions = data.get("data", data.get("positions", []))
            return positions if isinstance(positions, list) else []
        except Exception as e:
            log.error(f"EdgeX positions error: {e}")
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
            log.info(f"[DRY RUN] EdgeX {side.value} {size_base:.6f} {symbol} @ ~${mark_price:.2f}")
            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=side, size=size_base, price=mark_price,
                fee_usd=size_usd * 0.0005, order_id="dry_run",
            )

        try:
            slippage = max_slippage_pct / 100
            limit_price = mark_price * (1 + slippage) if side == Side.LONG else mark_price * (1 - slippage)

            result = await self._post("/api/v1/private/order", {
                "symbol": self._edgex_symbol(symbol),
                "side": "BUY" if side == Side.LONG else "SELL",
                "type": "MARKET",
                "size": f"{size_base:.6f}",
                "price": f"{limit_price:.2f}",
                "leverage": str(int(leverage)),
            })
            order_data = result.get("data", result)
            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=side, size=size_base, price=mark_price,
                fee_usd=size_usd * 0.0005,
                order_id=str(order_data.get("orderId", "")),
                raw=result,
            )
        except Exception as e:
            log.error(f"EdgeX trade error: {e}")
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=side, size=0, price=0, fee_usd=0, error=str(e),
            )

    async def close_position(
        self, symbol: str, side: Side, size: Optional[float] = None,
    ) -> TradeResult:
        opposite = Side.SHORT if side == Side.LONG else Side.LONG
        mark_price = await self.get_mark_price(symbol) or 0

        if size is None:
            positions = await self.get_open_positions()
            edgex_sym = self._edgex_symbol(symbol)
            for p in positions:
                if p.get("symbol") == edgex_sym:
                    size = abs(float(p.get("size", p.get("positionSize", 0))))
                    break

        if not size:
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=opposite, size=0, price=0, fee_usd=0,
                error="No position found",
            )

        size_usd = size * mark_price

        if settings.dry_run:
            log.info(f"[DRY RUN] EdgeX close {side.value} {size:.6f} {symbol}")
            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=opposite, size=size, price=mark_price,
                fee_usd=size_usd * 0.0005, order_id="dry_run_close",
            )

        try:
            result = await self._post("/api/v1/private/order", {
                "symbol": self._edgex_symbol(symbol),
                "side": "BUY" if opposite == Side.LONG else "SELL",
                "type": "MARKET",
                "size": str(size),
                "reduceOnly": True,
            })
            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=opposite, size=size, price=mark_price,
                fee_usd=size_usd * 0.0005, raw=result,
            )
        except Exception as e:
            log.error(f"EdgeX close error: {e}")
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=opposite, size=0, price=0, fee_usd=0, error=str(e),
            )

    async def estimate_fees(self, symbol: str, size_usd: float) -> float:
        # EdgeX: taker ~5 bps, round-trip ~10 bps
        return size_usd * 0.001
