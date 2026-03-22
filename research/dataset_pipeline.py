"""Dataset capture + compilation utilities for offline strategy research.

The pipeline has two stages:
1. Capture raw funding-rate snapshots from live connectors into JSONL.
2. Compile raw snapshots into an opportunities CSV suitable for research/backtest.py.
"""

from __future__ import annotations

import asyncio
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.models import ArbOpportunity, Platform

RAW_DIR = Path(__file__).resolve().parent / "data" / "raw"
COMPILED_DIR = Path(__file__).resolve().parent / "data"


class SnapshotRecorder:
    """Append-only recorder for raw funding snapshots and derived opportunities."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = Path(base_dir) if base_dir else RAW_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _day_path(self, prefix: str, ts: datetime) -> Path:
        return self.base_dir / f"{prefix}_{ts.strftime('%Y-%m-%d')}.jsonl"

    async def record(self, aggregator, opportunities: List[ArbOpportunity], balances: Optional[Dict[str, dict]] = None) -> Dict[str, Path]:
        ts = aggregator.last_update or datetime.utcnow()
        rates_path = self._day_path("rates", ts)
        opps_path = self._day_path("opportunities", ts)

        rate_rows = []
        for symbol, platform_rates in aggregator.rates.items():
            for platform, fr in platform_rates.items():
                rate_rows.append({
                    "captured_at": ts.isoformat(),
                    "symbol": symbol,
                    "platform": platform.value,
                    "rate_hourly": fr.rate_hourly,
                    "rate_annualised": fr.rate_annualised,
                    "funding_timestamp": fr.timestamp.isoformat(),
                })

        opp_rows = []
        for opp in opportunities:
            opp_rows.append({
                "captured_at": ts.isoformat(),
                "symbol": opp.symbol,
                "long_platform": opp.long_platform.value,
                "short_platform": opp.short_platform.value,
                "long_rate_ann": opp.long_rate_ann,
                "short_rate_ann": opp.short_rate_ann,
                "spread_ann": opp.spread_ann,
                "estimated_profit_daily_usd": opp.estimated_profit_daily_usd,
                "estimated_fees_usd": opp.estimated_fees_usd,
                "net_profit_daily_usd": opp.net_profit_daily_usd,
                "balances": balances or {},
            })

        with rates_path.open("a") as f:
            for row in rate_rows:
                f.write(json.dumps(row) + "\n")

        with opps_path.open("a") as f:
            for row in opp_rows:
                f.write(json.dumps(row) + "\n")

        return {"rates": rates_path, "opportunities": opps_path}


async def collect_balances(connectors: Dict[Platform, object]) -> Dict[str, dict]:
    async def one(platform: Platform, conn) -> tuple:
        try:
            bal = await conn.get_balance()
            return platform.value, {
                "equity_usd": bal.equity_usd,
                "free_margin_usd": bal.free_margin_usd,
                "used_margin_usd": bal.used_margin_usd,
                "unrealised_pnl_usd": bal.unrealised_pnl_usd,
                "timestamp": bal.timestamp.isoformat(),
            }
        except Exception as e:
            return platform.value, {"error": str(e)}

    pairs = await asyncio.gather(*[one(p, c) for p, c in connectors.items()])
    return dict(pairs)


def load_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_all_raw(base_dir: Optional[Path] = None) -> Dict[str, List[dict]]:
    root = Path(base_dir) if base_dir else RAW_DIR
    return {
        "rates": [row for p in sorted(root.glob("rates_*.jsonl")) for row in load_jsonl(p)],
        "opportunities": [row for p in sorted(root.glob("opportunities_*.jsonl")) for row in load_jsonl(p)],
    }


def _hours_between(a: datetime, b: datetime) -> float:
    return max(0.0, (b - a).total_seconds() / 3600.0)


def compile_opportunities_dataset(
    raw_dir: Optional[Path] = None,
    output_csv: Optional[Path] = None,
    horizon_hours: float = 24.0,
    min_future_points: int = 1,
) -> Path:
    raw = load_all_raw(raw_dir)
    rates = raw["rates"]
    opps = raw["opportunities"]

    grouped_rates: Dict[tuple, List[dict]] = defaultdict(list)
    for row in rates:
        grouped_rates[(row["symbol"], row["platform"])].append(row)

    for rows in grouped_rates.values():
        rows.sort(key=lambda r: r["captured_at"])

    compiled = []
    for opp in opps:
        t0 = datetime.fromisoformat(opp["captured_at"])
        horizon_end = t0 + timedelta(hours=horizon_hours)

        long_series = grouped_rates.get((opp["symbol"], opp["long_platform"]), [])
        short_series = grouped_rates.get((opp["symbol"], opp["short_platform"]), [])

        future_long = [r for r in long_series if t0 < datetime.fromisoformat(r["captured_at"]) <= horizon_end]
        future_short = [r for r in short_series if t0 < datetime.fromisoformat(r["captured_at"]) <= horizon_end]

        points = min(len(future_long), len(future_short))
        if points < min_future_points:
            continue

        realized_spreads = []
        persistence_hours = 0.0
        prev_t = t0
        for lrow, srow in zip(future_long[:points], future_short[:points]):
            ts = min(datetime.fromisoformat(lrow["captured_at"]), datetime.fromisoformat(srow["captured_at"]))
            spread_ann = (srow["rate_annualised"] - lrow["rate_annualised"]) * 100
            realized_spreads.append(spread_ann)
            if spread_ann > 0:
                persistence_hours += _hours_between(prev_t, ts)
            prev_t = ts

        avg_realized_spread_ann = sum(realized_spreads) / len(realized_spreads)
        notional_usd = 1000.0
        balances = opp.get("balances") or {}
        long_bal = balances.get(opp["long_platform"], {})
        short_bal = balances.get(opp["short_platform"], {})
        if isinstance(long_bal, dict) and isinstance(short_bal, dict):
            free_margins = [x.get("free_margin_usd") for x in (long_bal, short_bal) if isinstance(x.get("free_margin_usd"), (int, float))]
            if len(free_margins) == 2:
                notional_usd = max(100.0, min(free_margins) * 0.5)

        hold_days = max(horizon_hours / 24.0, 1e-9)
        realized_gross = (avg_realized_spread_ann / 100.0 / 365.0) * notional_usd * hold_days
        realized_net = realized_gross - float(opp.get("estimated_fees_usd", 0.0))

        spread_now = float(opp["spread_ann"])
        spread_ratio = avg_realized_spread_ann / spread_now if spread_now > 0 else 0.0
        execution_risk = max(0.0, min(1.0, 1.0 - spread_ratio))

        compiled.append({
            "timestamp": opp["captured_at"],
            "symbol": opp["symbol"],
            "long_platform": opp["long_platform"],
            "short_platform": opp["short_platform"],
            "long_rate_ann": float(opp["long_rate_ann"]),
            "short_rate_ann": float(opp["short_rate_ann"]),
            "spread_ann": spread_now,
            "estimated_profit_daily_usd": float(opp["estimated_profit_daily_usd"]),
            "estimated_fees_usd": float(opp["estimated_fees_usd"]),
            "net_profit_daily_usd": float(opp["net_profit_daily_usd"]),
            "notional_usd": round(notional_usd, 6),
            "expected_hold_hours": horizon_hours,
            "spread_persistence_hours": round(persistence_hours, 6),
            "execution_risk": round(execution_risk, 6),
            "slippage_bps": 5.0,
            "symbol_score": 1.0,
            "pair_score": round(min(1.25, max(0.5, spread_ratio)), 6),
            "realized_net_usd": round(realized_net, 6),
        })

    output = Path(output_csv) if output_csv else COMPILED_DIR / "opportunities_compiled.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "timestamp", "symbol", "long_platform", "short_platform", "long_rate_ann", "short_rate_ann",
        "spread_ann", "estimated_profit_daily_usd", "estimated_fees_usd", "net_profit_daily_usd",
        "notional_usd", "expected_hold_hours", "spread_persistence_hours", "execution_risk",
        "slippage_bps", "symbol_score", "pair_score", "realized_net_usd",
    ]
    with output.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(compiled)
    return output


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Compile raw snapshot JSONL into opportunities CSV")
    parser.add_argument("--raw-dir", default=str(RAW_DIR))
    parser.add_argument("--output", default=str(COMPILED_DIR / "opportunities_compiled.csv"))
    parser.add_argument("--horizon-hours", type=float, default=24.0)
    parser.add_argument("--min-future-points", type=int, default=1)
    args = parser.parse_args()

    output_path = compile_opportunities_dataset(
        raw_dir=Path(args.raw_dir),
        output_csv=Path(args.output),
        horizon_hours=args.horizon_hours,
        min_future_points=args.min_future_points,
    )
    print(str(output_path))


if __name__ == "__main__":
    main()
