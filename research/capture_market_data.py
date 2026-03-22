#!/usr/bin/env python3
"""Capture one raw market snapshot for research dataset building."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import create_connectors, initialise_connectors, shutdown_connectors
from engine.aggregator import FundingRateAggregator
from research.dataset_pipeline import SnapshotRecorder, collect_balances


async def _main() -> None:
    connectors = create_connectors()
    connectors = await initialise_connectors(connectors)
    try:
        aggregator = FundingRateAggregator(connectors)
        await aggregator.refresh_all_rates()
        opportunities = await aggregator.find_opportunities()
        balances = await collect_balances({c.platform: c for c in connectors})
        recorder = SnapshotRecorder()
        paths = await recorder.record(aggregator, opportunities, balances=balances)
        print(f"rates: {paths['rates']}")
        print(f"opportunities: {paths['opportunities']}")
        print(f"symbols: {len(aggregator.rates)}")
        print(f"opportunities_count: {len(opportunities)}")
    finally:
        await shutdown_connectors(connectors)


if __name__ == "__main__":
    asyncio.run(_main())
