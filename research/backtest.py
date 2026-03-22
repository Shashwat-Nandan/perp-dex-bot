"""Offline research harness for funding-rate arbitrage strategy iteration.

Expected input: a CSV of historical candidate opportunities. The strategy logic
lives in engine/strategy.py and can be mutated independently of the simulator.

CSV columns:
- timestamp
- symbol
- long_platform
- short_platform
- long_rate_ann
- short_rate_ann
- spread_ann
- estimated_profit_daily_usd
- estimated_fees_usd
- net_profit_daily_usd
Optional columns:
- notional_usd
- expected_hold_hours
- spread_persistence_hours
- execution_risk
- slippage_bps
- symbol_score
- pair_score
- realized_net_usd
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.strategy import StrategyContext, StrategyPolicy
from utils.models import ArbOpportunity, Platform


@dataclass
class BacktestTrade:
    timestamp: str
    symbol: str
    long_platform: str
    short_platform: str
    score: float
    expected_net_usd: float
    realized_net_usd: float
    should_trade: bool
    reasons: List[str]


def _float(row: Dict[str, str], key: str, default: float) -> float:
    value = row.get(key, "")
    if value in (None, ""):
        return default
    return float(value)


def load_rows(csv_path: Path) -> List[Dict[str, str]]:
    with csv_path.open("r", newline="") as f:
        return list(csv.DictReader(f))


def build_opportunity(row: Dict[str, str]) -> ArbOpportunity:
    return ArbOpportunity(
        symbol=row["symbol"],
        long_platform=Platform(row["long_platform"]),
        short_platform=Platform(row["short_platform"]),
        long_rate_ann=float(row["long_rate_ann"]),
        short_rate_ann=float(row["short_rate_ann"]),
        spread_ann=float(row["spread_ann"]),
        estimated_profit_daily_usd=float(row["estimated_profit_daily_usd"]),
        estimated_fees_usd=float(row["estimated_fees_usd"]),
        net_profit_daily_usd=float(row["net_profit_daily_usd"]),
    )


def build_context(row: Dict[str, str]) -> StrategyContext:
    return StrategyContext(
        symbol_score=_float(row, "symbol_score", 1.0),
        pair_score=_float(row, "pair_score", 1.0),
        expected_hold_hours=_float(row, "expected_hold_hours", 24.0),
        spread_persistence_hours=_float(row, "spread_persistence_hours", 24.0),
        execution_risk=_float(row, "execution_risk", 0.10),
        slippage_bps=_float(row, "slippage_bps", 5.0),
        opportunity_cost_usd=_float(row, "opportunity_cost_usd", 0.0),
    )


def run_backtest(rows: List[Dict[str, str]]) -> Dict:
    policy = StrategyPolicy()
    trades: List[BacktestTrade] = []
    equity_curve: List[float] = []
    cumulative_pnl = 0.0

    for row in rows:
        opp = build_opportunity(row)
        context = build_context(row)
        size_usd = _float(row, "notional_usd", 1000.0)
        decision = policy.score_opportunity(opp, size_usd=size_usd, context=context)

        realized_default = decision.expected_net_usd if decision.should_trade else 0.0
        realized_net = _float(row, "realized_net_usd", realized_default)
        if not decision.should_trade:
            realized_net = 0.0

        cumulative_pnl += realized_net
        equity_curve.append(cumulative_pnl)
        trades.append(
            BacktestTrade(
                timestamp=row.get("timestamp", ""),
                symbol=opp.symbol,
                long_platform=opp.long_platform.value,
                short_platform=opp.short_platform.value,
                score=decision.score,
                expected_net_usd=decision.expected_net_usd,
                realized_net_usd=realized_net,
                should_trade=decision.should_trade,
                reasons=decision.reasons,
            )
        )

    traded = [t for t in trades if t.should_trade]
    realized = [t.realized_net_usd for t in traded]
    wins = [x for x in realized if x > 0]
    losses = [x for x in realized if x < 0]

    peak = float("-inf")
    max_drawdown = 0.0
    for point in equity_curve:
        peak = max(peak, point)
        max_drawdown = min(max_drawdown, point - peak)

    net_pnl = sum(realized)
    avg_trade = mean(realized) if realized else 0.0
    win_rate = (len(wins) / len(realized)) if realized else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses else (999.0 if wins else 0.0)

    score = net_pnl + max_drawdown * 0.5

    return {
        "score": round(score, 6),
        "net_pnl_usd": round(net_pnl, 6),
        "max_drawdown_usd": round(abs(max_drawdown), 6),
        "trades_considered": len(trades),
        "trades_taken": len(traded),
        "win_rate": round(win_rate, 6),
        "avg_trade_usd": round(avg_trade, 6),
        "profit_factor": round(profit_factor, 6),
        "sample_symbols": sorted({t.symbol for t in traded})[:10],
        "top_trades": [asdict(t) for t in sorted(trades, key=lambda x: x.realized_net_usd, reverse=True)[:5]],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run offline strategy backtest")
    parser.add_argument("--input", default="research/data/opportunities_sample.csv")
    parser.add_argument("--output", default="research/out/latest_backtest.json")
    args = parser.parse_args()

    rows = load_rows(Path(args.input))
    report = run_backtest(rows)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2))

    print(f"score: {report['score']:.6f}")
    print(f"net_pnl_usd: {report['net_pnl_usd']:.6f}")
    print(f"max_drawdown_usd: {report['max_drawdown_usd']:.6f}")
    print(f"trades_taken: {report['trades_taken']}")
    print(f"win_rate: {report['win_rate']:.6f}")
    print(f"profit_factor: {report['profit_factor']:.6f}")


if __name__ == "__main__":
    main()
