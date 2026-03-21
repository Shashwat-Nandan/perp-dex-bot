"""
Aster DEX connector (formerly ApolloX / APX).
Uses REST API V3 with EIP-712 signature authentication.
Multi-chain: BNB Chain, Arbitrum, Ethereum.
"""

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

log = get_logger("connector.aster")

ASTER_API_BASE = "https://fapi.asterdex.com"
ASTER_WS_BASE = "wss://fstream.asterdex.com"


class AsterConnector(BaseConnector):
    platform = Platform.ASTER

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._api_key = settings.platform_keys.aster_api_key
        self._api_secret = settings.platform_keys.aster_api_secret
        self._private_key = settings.wallet.evm_private_key
        self._address = settings.wallet.evm_public_key
        self._symbols: List[str] = []
        self._symbol_info: Dict[str, dict] = {}

    async def initialise(self) -> None:
        self._session = aiohttp.ClientSession()
        await self._load_exchange_info()
        log.info(f"Aster initialised – {len(self._symbols)} markets")

    async def shutdown(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _sign_hmac(self, query_string: str) -> str:
        """HMAC-SHA256 signature for v1 auth."""
        if not self._api_secret:
            return ""
        return hmac.new(
            self._api_secret.encode(), query_string.encode(), hashlib.sha256
        ).hexdigest()

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["X-MBX-APIKEY"] = self._api_key
        return h

    async def _get(self, path: str, params: dict = None, signed: bool = False) -> dict:
        url = f"{ASTER_API_BASE}{path}"
        params = params or {}
        if signed:
            params["timestamp"] = str(int(time.time() * 1000))
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            params["signature"] = self._sign_hmac(qs)
        async with self._session.get(url, params=params, headers=self._headers()) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _post(self, path: str, data: dict = None, signed: bool = False) -> dict:
        url = f"{ASTER_API_BASE}{path}"
        data = data or {}
        if signed:
            data["timestamp"] = str(int(time.time() * 1000))
            qs = "&".join(f"{k}={v}" for k, v in data.items())
            data["signature"] = self._sign_hmac(qs)
        # Aster uses Binance-compatible API: signed POST params go as
        # query string, not JSON body.
        async with self._session.post(url, params=data, headers=self._headers()) as resp:
            if resp.status >= 400:
                body = await resp.text()
                log.error(f"Aster POST {path} failed ({resp.status}): {body}")
                resp.raise_for_status()
            return await resp.json()

    async def _load_exchange_info(self):
        try:
            data = await self._get("/fapi/v1/exchangeInfo")
            symbols_data = data.get("symbols", [])
            for s in symbols_data:
                sym = self.normalise_symbol(s.get("symbol", ""))
                if s.get("contractType") == "PERPETUAL" and sym:
                    self._symbols.append(sym)
                    self._symbol_info[sym] = s
        except Exception as e:
            log.warning(f"Aster exchange info error: {e}")
            self._symbols = ["BTC", "ETH", "SOL", "BNB", "ARB"]

    def _aster_symbol(self, symbol: str) -> str:
        return f"{symbol.upper()}USDT"

    async def get_available_symbols(self) -> List[str]:
        return self._symbols

    async def get_funding_rate(self, symbol: str) -> Optional[FundingRate]:
        try:
            data = await self._get("/fapi/v1/premiumIndex", {
                "symbol": self._aster_symbol(symbol)
            })
            rate = float(data.get("lastFundingRate", 0))
            # Aster uses 8-hour funding periods
            rate_hourly = rate / 8
            return FundingRate(
                platform=self.platform,
                symbol=symbol.upper(),
                rate_hourly=rate_hourly,
                rate_annualised=rate_hourly * 8760,
                raw=data,
            )
        except Exception as e:
            log.debug(f"Aster funding rate error for {symbol}: {e}")
            return None

    async def get_all_funding_rates(self) -> List[FundingRate]:
        try:
            # Batch endpoint for all premium indices
            data = await self._get("/fapi/v1/premiumIndex")
            if not isinstance(data, list):
                data = [data]
            rates = []
            for item in data:
                sym = self.normalise_symbol(item.get("symbol", ""))
                if sym:
                    rate = float(item.get("lastFundingRate", 0))
                    rate_hourly = rate / 8
                    rates.append(FundingRate(
                        platform=self.platform,
                        symbol=sym,
                        rate_hourly=rate_hourly,
                        rate_annualised=rate_hourly * 8760,
                        raw=item,
                    ))
            return rates
        except Exception as e:
            log.error(f"Aster all funding rates error: {e}")
            return []

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        try:
            data = await self._get("/fapi/v1/premiumIndex", {
                "symbol": self._aster_symbol(symbol)
            })
            return float(data.get("markPrice", 0))
        except Exception as e:
            log.debug(f"Aster mark price error for {symbol}: {e}")
            return None

    async def get_balance(self) -> AccountBalance:
        data = await self._get("/fapi/v2/balance", signed=True)
        usdt_balance = next(
            (b for b in data if b.get("asset") == "USDT"), {}
        ) if isinstance(data, list) else data

        log.debug("Aster raw USDT balance entry: %s", usdt_balance)

        wallet_balance = float(usdt_balance.get("balance", 0))
        available_balance = float(usdt_balance.get("availableBalance", 0))
        # Aster may return balance=0 while availableBalance is correct;
        # use availableBalance as equity fallback in that case.
        equity = wallet_balance if wallet_balance > 0 else available_balance

        bal = AccountBalance(
            platform=self.platform,
            equity_usd=equity,
            free_margin_usd=available_balance,
            used_margin_usd=max(equity - available_balance, 0),
            unrealised_pnl_usd=float(usdt_balance.get("crossUnPnl", 0)),
        )

        if wallet_balance == 0 and available_balance > 0:
            log.info(
                "Aster wallet balance=0, using availableBalance=%.2f as equity",
                available_balance,
            )
        elif equity == 0 and available_balance == 0:
            log.warning(
                "Aster balance returned zeros — raw response: %s",
                usdt_balance,
            )

        return bal

    async def get_open_positions(self) -> List[Dict]:
        try:
            data = await self._get("/fapi/v2/positionRisk", signed=True)
            return [p for p in data if float(p.get("positionAmt", 0)) != 0] if isinstance(data, list) else []
        except Exception as e:
            log.error(f"Aster positions error: {e}")
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
        aster_sym = self._aster_symbol(symbol)

        if settings.dry_run:
            log.info(f"[DRY RUN] Aster {side.value} {size_base:.6f} {symbol} @ ~${mark_price:.2f}")
            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=side, size=size_base, price=mark_price,
                fee_usd=size_usd * 0.0004, order_id="dry_run",
            )

        try:
            # Set leverage (skip if 1x — it's the default)
            if leverage > 1:
                await self._post("/fapi/v1/leverage", {
                    "symbol": aster_sym,
                    "leverage": int(leverage),
                }, signed=True)

            # Place market order
            result = await self._post("/fapi/v1/order", {
                "symbol": aster_sym,
                "side": "BUY" if side == Side.LONG else "SELL",
                "type": "MARKET",
                "quantity": f"{size_base:.8f}",
            }, signed=True)

            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=side, size=size_base, price=mark_price,
                fee_usd=size_usd * 0.0004,
                order_id=str(result.get("orderId", "")),
                raw=result,
            )
        except Exception as e:
            log.error(f"Aster trade error: {e}")
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
            aster_sym = self._aster_symbol(symbol)
            for p in positions:
                if p.get("symbol") == aster_sym:
                    size = abs(float(p.get("positionAmt", 0)))
                    break

        if not size:
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=opposite, size=0, price=0, fee_usd=0,
                error="No position found",
            )

        size_usd = size * mark_price

        if settings.dry_run:
            log.info(f"[DRY RUN] Aster close {side.value} {size:.6f} {symbol}")
            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=opposite, size=size, price=mark_price,
                fee_usd=size_usd * 0.0004, order_id="dry_run_close",
            )

        try:
            result = await self._post("/fapi/v1/order", {
                "symbol": self._aster_symbol(symbol),
                "side": "BUY" if opposite == Side.LONG else "SELL",
                "type": "MARKET",
                "quantity": f"{size:.8f}",
                "reduceOnly": "true",
            }, signed=True)
            return TradeResult(
                success=True, platform=self.platform, symbol=symbol,
                side=opposite, size=size, price=mark_price,
                fee_usd=size_usd * 0.0004,
                raw=result,
            )
        except Exception as e:
            log.error(f"Aster close error: {e}")
            return TradeResult(
                success=False, platform=self.platform, symbol=symbol,
                side=opposite, size=0, price=0, fee_usd=0, error=str(e),
            )

    async def estimate_fees(self, symbol: str, size_usd: float) -> float:
        # Aster: taker 4 bps, round-trip 8 bps
        return size_usd * 0.0008
