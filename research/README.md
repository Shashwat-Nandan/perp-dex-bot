# Research Harness

This directory turns `perp-dex-bot` into a safer, optimizable research system.

## Goal

Optimize strategy logic for **risk-adjusted profitability** before changing live execution.

## Core idea

- Stable simulator/evaluator: `research/backtest.py`
- Single preferred mutation surface: `engine/strategy.py`
- Result log: `research/results.tsv`
- Agent instructions: `research/program.md`

## Input data

The backtest currently accepts a CSV of historical or synthetic candidate opportunities.
Start with `research/data/opportunities_sample.csv` and replace it over time with captured real snapshots.

### Recommended future dataset columns

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
- notional_usd
- expected_hold_hours
- spread_persistence_hours
- execution_risk
- slippage_bps
- symbol_score
- pair_score
- realized_net_usd

## Run

```bash
python research/backtest.py --input research/data/opportunities_sample.csv
```

## Printed metrics

- `score` — primary keep/discard metric
- `net_pnl_usd`
- `max_drawdown_usd`
- `trades_taken`
- `win_rate`
- `profit_factor`

## Safety boundary

The research loop should mutate `engine/strategy.py` first.
Do **not** let an autonomous loop edit connector auth, order placement, or wallet handling until the offline loop proves useful.
