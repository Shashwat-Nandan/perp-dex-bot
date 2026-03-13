"""Tests for the Flask dashboard API."""

import json
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime

from dashboard.app import app
from utils.models import ArbOpportunity, Platform, PositionStatus


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


class TestHealthEndpoint:
    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert "timestamp" in data


class TestApiWithoutEngine:
    def test_status_returns_503_without_engine(self, client):
        with patch("dashboard.app._engine", None):
            resp = client.get("/api/status")
        assert resp.status_code == 503

    def test_rates_returns_503_without_engine(self, client):
        with patch("dashboard.app._engine", None):
            resp = client.get("/api/rates")
        assert resp.status_code == 503

    def test_opportunities_returns_503_without_engine(self, client):
        with patch("dashboard.app._engine", None):
            resp = client.get("/api/opportunities")
        assert resp.status_code == 503

    def test_positions_returns_503_without_engine(self, client):
        with patch("dashboard.app._engine", None):
            resp = client.get("/api/positions")
        assert resp.status_code == 503


class TestApiWithEngine:
    def _mock_engine(self):
        engine = MagicMock()
        engine.get_status.return_value = {
            "cycle_count": 5,
            "open_positions": 2,
            "dry_run": True,
        }
        engine.aggregator.get_rate_summary.return_value = [
            {
                "symbol": "BTC",
                "platform": "hyperliquid",
                "rate_hourly_pct": 0.01,
                "rate_ann_pct": 87.6,
                "timestamp": datetime.utcnow().isoformat(),
            }
        ]
        engine.last_opportunities = [
            ArbOpportunity(
                symbol="BTC",
                long_platform=Platform.HYPERLIQUID,
                short_platform=Platform.ASTER,
                long_rate_ann=5.0,
                short_rate_ann=35.0,
                spread_ann=30.0,
                estimated_profit_daily_usd=1.5,
                estimated_fees_usd=0.1,
                net_profit_daily_usd=1.4,
            )
        ]
        engine.position_manager.get_stats.return_value = {
            "open_positions": 2,
            "closed_positions": 3,
            "total_pnl_usd": 50.0,
        }
        return engine

    def test_status_returns_engine_status(self, client):
        engine = self._mock_engine()
        with patch("dashboard.app._engine", engine):
            resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["cycle_count"] == 5

    def test_rates_returns_rate_summary(self, client):
        engine = self._mock_engine()
        with patch("dashboard.app._engine", engine):
            resp = client.get("/api/rates")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["symbol"] == "BTC"

    def test_opportunities_returns_list(self, client):
        engine = self._mock_engine()
        with patch("dashboard.app._engine", engine):
            resp = client.get("/api/opportunities")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["symbol"] == "BTC"
        assert data[0]["spread_ann"] == 30.0
        assert data[0]["long_platform"] == "hyperliquid"
        assert data[0]["short_platform"] == "aster"

    def test_positions_returns_stats(self, client):
        engine = self._mock_engine()
        with patch("dashboard.app._engine", engine):
            resp = client.get("/api/positions")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["open_positions"] == 2

    def test_index_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"html" in resp.data.lower()
