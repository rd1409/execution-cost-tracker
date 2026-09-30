# Execution Cost Tracker

A small, dependency-free Python module for recording trade executions and measuring what they cost: commissions, fees, and slippage against a benchmark price.

## What it measures

For each execution:

| Metric | Definition |
|---|---|
| `explicit_cost` | commission + fees |
| `slippage_cost` | (fill price − benchmark price) × quantity, signed so a worse fill is a positive cost for both buys and sells |
| `total_cost` | explicit cost + slippage |
| `*_bps` | the cost as basis points of benchmark notional |

The benchmark can be whatever you measure against: arrival mid, decision price, VWAP, and so on.

## Install

```bash
pip install -e ".[dev]"
```

## Usage

```python
from execution_cost_tracker import Execution, ExecutionCostTracker, Side

tracker = ExecutionCostTracker()
tracker.record(Execution("AAPL", Side.BUY, 100, price=190.10, benchmark_price=190.00, commission=1.00, venue="BrokerA"))
tracker.record(Execution("AAPL", Side.SELL, 50, price=191.00, benchmark_price=191.20, commission=0.50, venue="BrokerB"))

summary = tracker.summary()
print(summary.total_cost, summary.total_cost_bps)

# Break costs down by symbol, venue, side, or any function of an execution
for venue, s in tracker.summary_by("venue").items():
    print(venue, round(s.total_cost_bps, 2))

# Narrow down before summarising
aapl_buys = tracker.filter(symbol="AAPL", side="buy")

# Save and reload
tracker.to_csv("executions.csv")
tracker = ExecutionCostTracker.from_csv("executions.csv")
```

## Tests

```bash
pytest
```
