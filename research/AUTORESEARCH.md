# Perp DEX Bot autoresearch setup

This repo now follows the same basic pattern as `karpathy/autoresearch`, adapted for offline strategy optimization.

## In-scope mutation surface

- **Edit:** `engine/strategy.py`
- **Read-only context:**
  - `research/backtest.py`
  - `research/README.md`
  - `research/program.md`
  - `engine/arb_engine.py`
  - `engine/aggregator.py`
  - `utils/models.py`

Do **not** autonomously edit live connector auth, wallet handling, or order execution paths.

## Metric

Primary metric: **`score`** from `research/backtest.py`

Higher is better.

Secondary metrics:
- higher `net_pnl_usd`
- lower `max_drawdown_usd`
- acceptable `trades_taken`
- acceptable `profit_factor`

## One baseline run

```bash
python3 research/run_experiment.py --description "baseline"
```

## One manual experiment

```bash
python3 research/run_experiment.py --description "persistence weighting tweak"
```

## Suggested agent prompt

```text
Read research/program.md and research/AUTORESEARCH.md.
Work only on engine/strategy.py.
Use python3 research/run_experiment.py --description "..." for every run.
Keep changes only when score improves meaningfully without adding fragility.
```

## Notes

- `research/results.tsv` is the experiment ledger.
- `research/out/latest_backtest.json` stores the latest structured run output.
- Current sample baseline on `research/data/opportunities_sample.csv` was 1.600000 at setup time.
- The current repo has local uncommitted work, so early results may show a `-dirty` commit suffix.
