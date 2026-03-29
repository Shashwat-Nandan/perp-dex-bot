"""
EdgeX perpetual DEX connector.
REST + WebSocket API on StarkEx (Ethereum L2).
API docs: https://edgex-1.gitbook.io/edgeX-documentation/api
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

EDGEX_API_BASE = "https://pro.edgex.exchange"
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
        # symbol -> contract metadata (includes contractId)
        self._contracts: Dict[str, dict] = {}
        # symbol -> contractId string (e.g. "10000001")
        self._contract_ids: Dict[str, str] = {}

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
        """Load contract metadata from EdgeX getMetaData endpoint."""
        try:
            data = await self._get("/api/v1/public/meta/getMetaData")
            # Response: {"code": "SUCCESS", "data": {"contractList": [...]}}
            contract_list = []
            if isinstance(data, dict):
                inner = data.get("data", data)
                if isinstance(inner, dict):
                    contract_list = inner.get("contractList", inner.get("contracts", []))
                elif isinstance(inner, list):
                    contract_list = inner

            for c in contract_list:
                contract_name = c.get("contractName", "")
                contract_id = str(c.get("contractId", ""))
                sym = self.normalise_symbol(contract_name)
                if sym and contract_id:
                    self._contracts[sym] = c
                    self._contract_ids[sym] = contract_id
                    self._symbols.append(sym)

            if not self._symbols:
                log.warning("EdgeX getMetaData returned no parseable contracts")
        except Exception as e:
            log.warning(f"EdgeX contracts load error: {e}")

    def _get_contract_id(self, symbol: str) -> Optional[str]:
        """Get the EdgeX contractId for a normalised symbol."""
        return self._contract_ids.get(symbol.upper())

    async def get_available_symbols(self) -> List[str]:
        return self._symbols

    async def get_funding_rate(self, symbol: str) -> Optional[FundingRate]:
        """Fetch funding rate for a single symbol using its contractId."""
        contract_id = self._get_contract_id(symbol)
        if not contract_id:
            return None
        try:
            data = await self._get(
                "/api/v1/public/funding/getLatestFundingRate",
                {"contractId": contract_id},
            )
            items = data.get("data", [])
            if not isinstance(items, list) or not items:
                return None
            item = items[0]
            rate = float(item.get("fundingRate", 0))
            # EdgeX funding interval is 4 hours (240 min)
            interval_hours = int(item.get("fundingRateIntervalMin", 240)) / 60
            rate_hourly = rate / interval_hours if interval_hours > 0 else rate / 4
            return FundingRate(
                platform=self.platform,
                symbol=symbol.upper(),
                rate_hourly=rate_hourly,
                rate_annualised=rate_hourly * 8760,
                raw=item,
            )
        except Exception as e:
            log.debug(f"EdgeX funding rate error for {symbol}: {e}")
            return None

    async def get_all_funding_rates(self) -> List[FundingRate]:
        """Fetch funding rates for all contracts (no batch endpoint — per-contract)."""
        if not self._symbols:
            log.warning("EdgeX: no contracts loaded, cannot fetch funding rates")
            return []

        tasks = [self.get_funding_rate(sym) for sym in self._symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        rates = []
        for r in results:
            if isinstance(r, FundingRate):
                rates.append(r)
            elif isinstance(r, Exception):
                log.debug(f"EdgeX funding rate fetch error: {r}")
        if not rates:
            log.warning(f"EdgeX: returned 0 rates (tried {len(self._symbols)} contracts)")
        return rates

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        """Fetch mark price from getTicker endpoint."""
        contract_id = self._get_contract_id(symbol)
        if not contract_id:
            return None
        try:
            data = await self._get(
                "/api/v1/public/quote/getTicker",
                {"contractId": contract_id},
            )
            items = data.get("data", [])
            if isinstance(items, list) and items:
                return float(items[0].get("markPrice", items[0].get("lastPrice", 0)))
            return None
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
                "contractId": self._get_contract_id(symbol),
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
            contract_id = self._get_contract_id(symbol)
            for p in positions:
                if str(p.get("contractId")) == contract_id:
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
                "contractId": self._get_contract_id(symbol),
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
