#!/usr/bin/env python3
"""Run one offline strategy experiment and append a TSV row.

This is a thin adapter around research/backtest.py so autoresearch-style agents
can iterate safely on engine/strategy.py and get consistent logs.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "research" / "results.tsv"
DEFAULT_INPUT = ROOT / "research" / "data" / "opportunities_sample.csv"
DEFAULT_OUTPUT = ROOT / "research" / "out" / "latest_backtest.json"


def git_short_head() -> str:
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
        ).strip()
        dirty = subprocess.call(
            ["git", "diff", "--quiet"], cwd=ROOT
        ) != 0 or subprocess.call(
            ["git", "diff", "--cached", "--quiet"], cwd=ROOT
        ) != 0
        return f"{head}-dirty" if dirty else head
    except Exception:
        return "unknown"


def ensure_results_header() -> None:
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    if not RESULTS.exists() or not RESULTS.read_text().strip():
        RESULTS.write_text("commit\tscore\tnet_pnl_usd\tmax_drawdown_usd\tstatus\tdescription\n")


def append_result(commit: str, report: dict, status: str, description: str) -> None:
    with RESULTS.open("a") as f:
        f.write(
            f"{commit}\t{report.get('score', 0.0):.6f}\t{report.get('net_pnl_usd', 0.0):.6f}\t{report.get('max_drawdown_usd', 0.0):.6f}\t{status}\t{description}\n"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one offline strategy experiment")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--description", default="manual experiment")
    parser.add_argument("--status", default="keep", choices=["keep", "discard", "crash"])
    args = parser.parse_args()

    ensure_results_header()

    cmd = [
        "python3",
        "research/backtest.py",
        "--input",
        args.input,
        "--output",
        args.output,
    ]
    result = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    print(result.stdout, end="")
    if result.returncode != 0:
        print(result.stderr, end="")
        report = {"score": 0.0, "net_pnl_usd": 0.0, "max_drawdown_usd": 0.0}
        append_result(git_short_head(), report, "crash", args.description)
        return result.returncode

    report = json.loads(Path(args.output).read_text())
    append_result(git_short_head(), report, args.status, args.description)
    print(f"logged_to: {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
