"""
Lightweight Flask dashboard for monitoring the arb bot.
"""

import json
from datetime import datetime
from flask import Flask, render_template, jsonify

from config import settings
from utils.logger import get_logger

log = get_logger("dashboard")

app = Flask(__name__, template_folder="templates")
app.secret_key = settings.dashboard.secret_key

# These will be set by the main scheduler when it starts
_engine = None


def set_engine(engine):
    global _engine
    _engine = engine


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    if not _engine:
        return jsonify({"error": "Engine not initialised"}), 503
    return jsonify(_engine.get_status())


@app.route("/api/rates")
def api_rates():
    if not _engine:
        return jsonify({"error": "Engine not initialised"}), 503
    return jsonify(_engine.aggregator.get_rate_summary())


@app.route("/api/opportunities")
def api_opportunities():
    if not _engine:
        return jsonify({"error": "Engine not initialised"}), 503
    opps = _engine.last_opportunities
    return jsonify([
        {
            "symbol": o.symbol,
            "long_platform": o.long_platform.value,
            "short_platform": o.short_platform.value,
            "long_rate_ann": round(o.long_rate_ann, 2),
            "short_rate_ann": round(o.short_rate_ann, 2),
            "spread_ann": round(o.spread_ann, 2),
            "net_daily_usd": round(o.net_profit_daily_usd, 2),
            "detected_at": o.detected_at.isoformat(),
        }
        for o in opps
    ])


@app.route("/api/positions")
def api_positions():
    if not _engine:
        return jsonify({"error": "Engine not initialised"}), 503
    return jsonify(_engine.position_manager.get_stats())


@app.route("/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()})


def run_dashboard():
    """Run the dashboard in a separate thread."""
    app.run(
        host=settings.dashboard.host,
        port=settings.dashboard.port,
        debug=False,
        use_reloader=False,
    )
