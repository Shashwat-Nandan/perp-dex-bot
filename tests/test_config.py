"""Tests for config.py settings loading."""

import os
import pytest
from unittest.mock import patch

from config import Settings, WalletConfig, RPCConfig, ArbParams, DashboardConfig


class TestSettingsDefaults:
    def test_dry_run_defaults_false(self):
        with patch.dict(os.environ, {}, clear=True):
            s = Settings()
        assert s.dry_run is False

    def test_log_level_defaults_info(self):
        with patch.dict(os.environ, {}, clear=True):
            s = Settings()
        assert s.log_level == "INFO"

    def test_log_file_default(self):
        with patch.dict(os.environ, {}, clear=True):
            s = Settings()
        assert s.log_file == "perp_arb_bot.log"


class TestArbParamsDefaults:
    def test_default_entry_threshold(self):
        with patch.dict(os.environ, {}, clear=True):
            params = ArbParams()
        assert params.entry_rate_diff_pct == 10.0

    def test_default_exit_threshold(self):
        with patch.dict(os.environ, {}, clear=True):
            params = ArbParams()
        assert params.exit_rate_diff_pct == 3.0

    def test_default_min_balance(self):
        with patch.dict(os.environ, {}, clear=True):
            params = ArbParams()
        assert params.min_balance_usd == 150.0

    def test_default_position_size(self):
        with patch.dict(os.environ, {}, clear=True):
            params = ArbParams()
        assert params.position_size_pct == 25.0

    def test_default_max_concurrent(self):
        with patch.dict(os.environ, {}, clear=True):
            params = ArbParams()
        assert params.max_concurrent_positions == 5

    def test_default_max_slippage(self):
        with patch.dict(os.environ, {}, clear=True):
            params = ArbParams()
        assert params.max_slippage_pct == 0.5

    def test_default_min_profit(self):
        with patch.dict(os.environ, {}, clear=True):
            params = ArbParams()
        assert params.min_profit_threshold_usd == 0.5


class TestArbParamsFromEnv:
    def test_reads_entry_threshold_from_env(self):
        with patch.dict(os.environ, {"ENTRY_FUNDING_RATE_DIFF_PCT": "30.0"}):
            params = ArbParams()
        assert params.entry_rate_diff_pct == 30.0

    def test_reads_max_positions_from_env(self):
        with patch.dict(os.environ, {"MAX_CONCURRENT_POSITIONS": "10"}):
            params = ArbParams()
        assert params.max_concurrent_positions == 10


class TestDashboardConfig:
    def test_default_host(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = DashboardConfig()
        assert cfg.host == "0.0.0.0"

    def test_port_reads_railway_port(self):
        with patch.dict(os.environ, {"PORT": "3000"}):
            cfg = DashboardConfig()
        assert cfg.port == 3000

    def test_default_port_when_no_env(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = DashboardConfig()
        assert cfg.port == 8080


class TestRPCConfig:
    def test_default_rpcs(self):
        with patch.dict(os.environ, {}, clear=True):
            rpc = RPCConfig()
        assert "arbitrum" in rpc.arbitrum.lower()
        assert "solana" in rpc.solana.lower() or "mainnet" in rpc.solana.lower()

    def test_custom_rpc_from_env(self):
        with patch.dict(os.environ, {"ARBITRUM_RPC_URL": "https://my-rpc.com"}):
            rpc = RPCConfig()
        assert rpc.arbitrum == "https://my-rpc.com"


class TestSettingsImmutable:
    def test_frozen_dataclass(self):
        with patch.dict(os.environ, {}, clear=True):
            s = Settings()
        with pytest.raises(Exception):
            s.dry_run = False
