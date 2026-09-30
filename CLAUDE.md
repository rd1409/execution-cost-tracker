# CLAUDE.md

Guidance for Claude when working in this repository.

## What this project is

Execution Cost Tracker is a small, dependency-free Python package that records trade executions and measures their costs: commissions, fees, and slippage against a benchmark price (arrival mid, decision price, VWAP, etc.).

## Layout

```
src/execution_cost_tracker/
    __init__.py   # public API: Execution, ExecutionCostTracker, CostSummary, Side
    models.py     # Execution (one fill) and CostSummary (aggregate) dataclasses
    tracker.py    # ExecutionCostTracker: record, filter, summarise, CSV I/O
tests/
    test_tracker.py
```

## Commands

```bash
pip install -e ".[dev]"   # install with pytest
pytest                    # run tests (pyproject sets pythonpath=src)
```

## Conventions

- **Cost sign:** a positive cost always means worse for the trader. Slippage is `side.sign * (price - benchmark_price) * quantity`, so paying up on a buy or selling low is positive; price improvement is negative.
- **Basis points** are measured against benchmark notional (`quantity * benchmark_price`) and multiplied by 10,000.
- `Execution` and `CostSummary` are frozen dataclasses. Derived metrics are read-only properties, not stored fields.
- `Execution` validates in `__post_init__`: quantity and prices must be positive, and commission and fees non-negative. `side` accepts a `Side` or a string such as `"buy"`.
- `ExecutionCostTracker.filter()` returns a new tracker and never mutates the original. `start` is inclusive and `end` is exclusive.
- Keep the core package free of third-party runtime dependencies. Standard library only.
- If you add a field to `Execution`, update `_CSV_FIELDS`, `to_csv`, and `from_csv` in `tracker.py` together, and extend the CSV round-trip test.
- Target Python 3.10+ and use type hints throughout.
- Every behaviour change comes with a test in `tests/`.
