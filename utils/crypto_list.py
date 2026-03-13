"""
Top 200 crypto projects by market cap - symbols to monitor.
Updated periodically via CoinGecko/CoinMarketCap API.
"""

import asyncio
import aiohttp
from typing import List
from .logger import get_logger

log = get_logger("crypto_list")

# Fallback static list of top 200 symbols (frequently traded on perp DEXes)
TOP_200_SYMBOLS: List[str] = [
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX", "DOT", "LINK",
    "TRX", "MATIC", "SHIB", "UNI", "LTC", "ATOM", "XLM", "BCH", "NEAR", "FIL",
    "APT", "ARB", "OP", "IMX", "INJ", "STX", "MKR", "AAVE", "GRT", "ALGO",
    "FTM", "SAND", "MANA", "AXS", "THETA", "EOS", "FLOW", "XTZ", "CFX", "NEO",
    "KAVA", "CAKE", "ROSE", "ZIL", "CHZ", "GALA", "ENJ", "BAT", "COMP", "CRV",
    "1INCH", "SUSHI", "YFI", "SNX", "ZRX", "DYDX", "LDO", "RPL", "FXS", "GMX",
    "PENDLE", "SSV", "RBN", "BLUR", "JTO", "JUP", "PYTH", "WIF", "BONK", "PEPE",
    "FLOKI", "ORDI", "RUNE", "TIA", "SEI", "SUI", "MINA", "ICP", "HBAR", "VET",
    "FET", "AGIX", "RNDR", "AR", "OCEAN", "KAS", "TAO", "WLD", "STRK", "ZK",
    "EIGEN", "ENA", "ETHFI", "REZ", "AEVO", "W", "DYM", "MANTA", "ALT", "PIXEL",
    "PORTAL", "SAGA", "ONDO", "BOME", "MEW", "BRETT", "POPCAT", "NEIRO", "TURBO",
    "PEOPLE", "JASMY", "MASK", "SKL", "CELO", "ONE", "QTUM", "IOTA", "ZEC", "DASH",
    "XMR", "WAVES", "ICX", "ONT", "RVN", "SC", "ZEN", "STORJ", "ANKR", "LRC",
    "CELR", "HOOK", "SFP", "GMT", "MAGIC", "APE", "LOOKS", "DAO", "CVX", "LQTY",
    "PERP", "BAND", "COTI", "RSR", "TRB", "UMA", "API3", "ALPHA", "NMR", "RLC",
    "BEL", "SXP", "DODO", "ACH", "LEVER", "COMBO", "MAV", "CYBER", "ARK", "FRONT",
    "PHB", "AMB", "GAS", "POLYX", "KEY", "OGN", "LOOM", "BICO", "T", "ERN",
    "CTSI", "PROM", "REQ", "BADGER", "CLV", "VOXEL", "HIGH", "AUCTION", "PROS",
    "TWT", "SLP", "C98", "DEGO", "POND", "CHESS", "DAR", "BSW", "MDT", "FIDA",
    "RAD", "RARE", "SUPER", "IDEX", "LINA", "UNFI", "TLM", "FORTH", "BOND", "KP3R",
]


async def fetch_top_200_from_coingecko() -> List[str]:
    """
    Fetch top 200 crypto symbols by market cap from CoinGecko free API.
    Falls back to the static list on failure.
    """
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": 200,
        "page": 1,
        "sparkline": "false",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    symbols = [coin["symbol"].upper() for coin in data]
                    log.info(f"Fetched {len(symbols)} symbols from CoinGecko")
                    return symbols
                else:
                    log.warning(f"CoinGecko returned status {resp.status}, using fallback list")
    except Exception as e:
        log.warning(f"Failed to fetch from CoinGecko: {e}, using fallback list")

    return TOP_200_SYMBOLS


def get_top_200_sync() -> List[str]:
    """Synchronous wrapper."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return TOP_200_SYMBOLS
        return loop.run_until_complete(fetch_top_200_from_coingecko())
    except RuntimeError:
        return TOP_200_SYMBOLS
