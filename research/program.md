# trading autoresearch program

This repo uses an `autoresearch`-style loop, but for a funding-rate arbitrage strategy.

## Scope

Primary mutation surface:
- `engine/strategy.py`

Read-only context:
- `research/backtest.py`
- `research/README.md`
- `utils/models.py`
- `engine/arb_engine.py`
- `engine/aggregator.py`

Do not modify live exchange connector auth or order execution code as part of the autonomous loop.

## Goal

Improve the primary offline metric:
- `score` from `research/backtest.py`

Secondary metrics:
- higher `net_pnl_usd`
- lower `max_drawdown_usd`
- acceptable `trades_taken`
- acceptable `profit_factor`

## Experiment command

Preferred wrapper:

```bash
python3 research/run_experiment.py --description "baseline"
```

Raw backtest command:

```bash
python3 research/backtest.py --input research/data/opportunities_sample.csv > run.log 2>&1
```

## Parse results

If using the raw command:

```bash
grep "^score:\|^net_pnl_usd:\|^max_drawdown_usd:\|^trades_taken:\|^win_rate:\|^profit_factor:" run.log
```

If using the wrapper, the run is also appended to `research/results.tsv` automatically.

## Logging results

Append tab-separated rows to `research/results.tsv`:

```text
commit	score	net_pnl_usd	max_drawdown_usd	status	description
```

Status values:
- `keep`
- `discard`
- `crash`

## Strategy ideas worth exploring

- better persistence weighting
- symbol-specific quality discounts
- pair-specific execution risk penalties
- faster rejection of low-quality trades
- size recommendations tied to confidence
- expected holding-period aware fee treatment

## Keep/discard rule

Keep only changes that improve `score` meaningfully without making `engine/strategy.py` messy or fragile.
If score improvement is negligible and complexity increases, discard.
