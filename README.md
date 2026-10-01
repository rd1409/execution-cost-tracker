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

## FX tracker

The package also tracks tokenised FX: it quotes EURC/USDC on Base from Uniswap v3, Uniswap v4, Aerodrome and the 0x aggregator. It then compares each quote with the EUR/USD reference mid and stores the results in SQLite.

```bash
pip install -e ".[dev]"
export BASE_RPC_URL=https://...      # optional; defaults to the public Base RPC
export ZEROX_API_KEY=...             # optional; needed for the 0x venue
python -m execution_cost_tracker.run --sizes 1000,10000,100000
```

Each cycle prints the following for every venue and size:

- bid and ask in USDC per EURC
- the cost of selling and of buying, in bps from the reference mid (positive means worse than mid)
- the round-trip spread in bps
- the venue mid's offset from the reference mid

Failed venues are listed with their errors and never stop the cycle. Everything is written to `fxtracker.db`; pass `--no-store` to skip writing.

The reference mid comes from the ECB rates on [Frankfurter](https://frankfurter.dev). These update once per working day, so treat the comparison against mid as approximate during the trading day. To use a live feed instead, implement `reference.ReferenceSource`.

## Web API and Vercel

`app.py` at the repo root is the entrypoint Vercel looks for. It loads the FastAPI app in `src/execution_cost_tracker/api.py`:

| Endpoint | What it does |
|---|---|
| `GET /api/health` | Liveness check |
| `GET /api/pairs` | Pairs, tokens and venues a front end can offer |
| `POST /api/quote` | On-demand quote, e.g. `{"pair": "EURC/USDC", "notional": 25000, "venues": ["uniswap_v3", "zerox"]}`. Not stored. |
| `GET /api/cron` | Scheduled run: quotes every pair, venue and size, then stores everything. Requires `Authorization: Bearer <CRON_SECRET>`. |

FastAPI also serves interactive docs at `/docs`, where you can try requests in the browser.

To run it locally:

```bash
pip install -e ".[dev]"
uvicorn app:app --reload        # then open http://127.0.0.1:8000/docs
```

To deploy:

1. In Vercel, choose **Add New → Project** and import this GitHub repo. Each push to `main` then redeploys.
2. Under **Project → Settings → Environment Variables**, set:
   - `BASE_RPC_URL`: your Base RPC provider URL
   - `ZEROX_API_KEY`: from dashboard.0x.org
   - `CRON_SECRET`: a random string of 16+ characters
   - `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN`: from a Turso database
   - `MAX_NOTIONAL`: optional cap on quote size (default 1,000,000)
3. `vercel.json` schedules `/api/cron` daily at 12:00 UTC. That is the most often the Hobby plan allows. On Pro, change the `schedule` (for example `*/15 * * * *`) and push.

Storage uses Turso when `TURSO_DATABASE_URL` is set and a local `fxtracker.db` otherwise. On Vercel it refuses to fall back to a local file, because files there don't persist.

## Tests

```bash
pytest
```
