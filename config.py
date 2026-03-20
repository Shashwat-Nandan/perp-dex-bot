"""
Central configuration module.
Loads .env and exposes typed settings used across the bot.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
_env_path = Path(__file__).parent / ".env"
load_dotenv(_env_path)


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_float(key: str, default: float = 0.0) -> float:
    return float(os.getenv(key, str(default)))


def _env_int(key: str, default: int = 0) -> int:
    return int(os.getenv(key, str(default)))


def _env_bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


@dataclass(frozen=True)
class WalletConfig:
    evm_private_key: str = field(repr=False, default_factory=lambda: _env("EVM_PRIVATE_KEY"))
    evm_public_key: str = field(default_factory=lambda: _env("EVM_PUBLIC_KEY"))
    solana_private_key: str = field(repr=False, default_factory=lambda: _env("SOLANA_PRIVATE_KEY"))


@dataclass(frozen=True)
class RPCConfig:
    arbitrum: str = field(default_factory=lambda: _env("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc"))
    ethereum: str = field(default_factory=lambda: _env("ETHEREUM_RPC_URL", "https://eth.llamarpc.com"))
    bsc: str = field(default_factory=lambda: _env("BSC_RPC_URL", "https://bsc-dataseed1.binance.org"))
    solana: str = field(default_factory=lambda: _env("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"))


@dataclass(frozen=True)
class PlatformKeys:
    """Delegated API credentials for each trading platform.

    These keys are generated from each platform's UI while logged in with the
    master EVM wallet (WalletConfig).  They are tied to the *same* account /
    address — no separate wallet or account is required.

    * Hyperliquid: optional — if blank the bot signs with the master EVM key.
    * Aster / EdgeX: required — HMAC key+secret (and STARK key for EdgeX).
    """

    # Hyperliquid
    hl_api_wallet_key: str = field(repr=False, default_factory=lambda: _env("HYPERLIQUID_API_WALLET_KEY"))
    hl_api_wallet_address: str = field(default_factory=lambda: _env("HYPERLIQUID_API_WALLET_ADDRESS"))
    # Aster
    aster_api_key: str = field(repr=False, default_factory=lambda: _env("ASTER_API_KEY"))
    aster_api_secret: str = field(repr=False, default_factory=lambda: _env("ASTER_API_SECRET"))
    # EdgeX
    edgex_api_key: str = field(repr=False, default_factory=lambda: _env("EDGEX_API_KEY"))
    edgex_api_secret: str = field(repr=False, default_factory=lambda: _env("EDGEX_API_SECRET"))
    edgex_stark_key: str = field(repr=False, default_factory=lambda: _env("EDGEX_STARK_PRIVATE_KEY"))


@dataclass(frozen=True)
class ArbParams:
    entry_rate_diff_pct: float = field(default_factory=lambda: _env_float("ENTRY_FUNDING_RATE_DIFF_PCT", 10.0))
    exit_rate_diff_pct: float = field(default_factory=lambda: _env_float("EXIT_FUNDING_RATE_DIFF_PCT", 3.0))
    min_balance_usd: float = field(default_factory=lambda: _env_float("MIN_ACCOUNT_BALANCE_USD", 100.0))
    position_size_pct: float = field(default_factory=lambda: _env_float("POSITION_SIZE_PCT", 25.0))
    max_concurrent_positions: int = field(default_factory=lambda: _env_int("MAX_CONCURRENT_POSITIONS", 5))
    max_slippage_pct: float = field(default_factory=lambda: _env_float("MAX_SLIPPAGE_PCT", 0.5))
    min_profit_threshold_usd: float = field(default_factory=lambda: _env_float("MIN_PROFIT_THRESHOLD_USD", 0.5))


@dataclass(frozen=True)
class AlertConfig:
    telegram_bot_token: str = field(repr=False, default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    discord_webhook_url: str = field(default_factory=lambda: _env("DISCORD_WEBHOOK_URL"))


@dataclass(frozen=True)
class DashboardConfig:
    host: str = field(default_factory=lambda: _env("DASHBOARD_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("PORT", _env_int("DASHBOARD_PORT", 8080)))
    secret_key: str = field(repr=False, default_factory=lambda: _env("DASHBOARD_SECRET_KEY", "change-me"))


@dataclass(frozen=True)
class SchedulerConfig:
    funding_rate_poll_interval: int = field(default_factory=lambda: _env_int("FUNDING_RATE_POLL_INTERVAL", 300))
    position_check_interval: int = field(default_factory=lambda: _env_int("POSITION_CHECK_INTERVAL", 60))


@dataclass(frozen=True)
class Settings:
    wallet: WalletConfig = field(default_factory=WalletConfig)
    rpc: RPCConfig = field(default_factory=RPCConfig)
    platform_keys: PlatformKeys = field(default_factory=PlatformKeys)
    arb: ArbParams = field(default_factory=ArbParams)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    dry_run: bool = field(default_factory=lambda: _env_bool("DRY_RUN", False))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    log_file: str = field(default_factory=lambda: _env("LOG_FILE", "perp_arb_bot.log"))


# Singleton settings instance
settings = Settings()
