# Research dataset pipeline

The old sample dataset is too small to support serious optimization. This pipeline fixes that.

## Design

Two layers:

1. **Raw capture** — immutable append-only JSONL snapshots from live connectors
2. **Compiled dataset** — derived CSV for `research/backtest.py`

Why this shape:
- raw data stays reusable
- feature engineering can evolve without recollecting market data
- labels can be recomputed with different holding horizons later

## Files

- `research/capture_market_data.py` — capture one live snapshot
- `research/dataset_pipeline.py` — compile raw snapshots into CSV
- `research/data/raw/rates_YYYY-MM-DD.jsonl` — per-platform funding snapshots
- `research/data/raw/opportunities_YYYY-MM-DD.jsonl` — derived opportunities at capture time
- `research/data/opportunities_compiled.csv` — compiled training/backtest dataset

## Capture one snapshot

```bash
python3 research/capture_market_data.py
```

## Capture repeatedly

For example every 15 minutes via cron or daemon wrapper. More frequent snapshots improve the forward-label quality.

## Compile dataset

```bash
python3 research/dataset_pipeline.py --horizon-hours 24
```

## What gets labeled

For each captured opportunity, the compiler looks forward over the chosen horizon and estimates:
- realized average spread
- spread persistence hours
- realized net USD after fees
- rough execution risk proxy based on spread decay

These are still approximations, but they are already much better than a hand-written toy CSV.

## Recommended next upgrades

- add mark price snapshots and realized basis moves
- log symbol liquidity / volume proxies per venue
- log platform-specific fee tiers and maker/taker estimates
- record whether the opportunity would have actually been tradable at available margin
- build a realized-close label based on actual future exit threshold crossing, not just fixed horizon average
