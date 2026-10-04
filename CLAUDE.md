# CLAUDE.md

Guidance for Claude when working in this repository.

## What this project is

Execution Cost Tracker is a Python package with two parts:

1. **Trade cost tracking.** It records trade executions and measures their costs: commissions, fees, and slippage against a benchmark price (arrival mid, decision price, VWAP, etc.).
2. **FX tracking.** It quotes tokenised FX pairs (EURC/USDC on Base) from on-chain venues and an aggregator. It then scores each quote against a traditional FX reference mid and stores the results in SQLite or Turso. A public page lets users price a trade on demand, and a daily scheduled run builds history.

## Layout

```
app.py               # Vercel entrypoint: puts src/ on the path, exposes `app`
vercel.json          # function maxDuration, excluded files, daily cron -> /api/cron
config.yaml          # registry: chains, tokens, pairs, limits, guardrails, quote band, cron sizes
public/index.html    # front-end page (single file, inline CSS/JS), served at /
tradingview/         # Pine script that sends bid/ask to the webhook (TradingView has no {{bid}}/{{ask}} placeholders)
src/execution_cost_tracker/
    api.py           # FastAPI routes only: /, /api/health, /api/config, /api/pairs, /api/quote, /api/cron, /api/tradingview/webhook
    service.py       # API logic, framework-free: QuoteParams validation, config_view, quote, guarded_quote, cron
    registry.py      # loads and validates config.yaml (FX_CONFIG env overrides the path)
    guardrails.py    # in-memory sliding-window rate limiter, TTL cache, client IP key
    market_hours.py  # FX hours: Monday 05:00 Sydney to Friday 17:00 New York (zoneinfo, DST-safe)
    __init__.py      # public API
    models.py        # Execution, CostSummary (trade costs); Token, Pair, Quote, ReferenceRate (FX)
    tracker.py       # ExecutionCostTracker: record, filter, summarise, CSV I/O
    chain.py         # web3 RPC setup (<CHAIN>_RPC_URL env), ABI loading, contract helper
    abis/            # minimal ABI JSON for quoters/routers/ERC-20
    venues/
        base.py      # Venue interface, SwapResult, VenueError, best_of()
        uniswap_v3.py   # QuoterV2 across fee tiers
        uniswap_v4.py   # V4Quoter across candidate pool keys
        aerodrome.py    # Slipstream quoter + classic router
        zerox.py        # 0x Swap API /swap/permit2/price (ZEROX_API_KEY)
    reference.py     # ReferenceSource protocol; Frankfurter (ECB daily) and Static sources
    metrics.py       # spread/bps math: pure functions, no network or I/O
    storage.py       # Turso (TURSO_DATABASE_URL) or local SQLite: reference_rates, quotes, failures, market_mids
    run.py           # one cycle: fetch all -> compute -> store -> print; CLI entry point
tests/
    test_tracker.py  # trade cost tracking
    test_metrics.py  # FX metrics
    test_fx_run.py   # venues (with fakes), reference parsing, storage, run cycle
    test_service.py  # API logic, cron auth, storage backends (offline)
    test_registry.py # config.yaml loading and validation
    test_guardrails.py # rate limiter, cache, guarded_quote (fake clock)
    test_market_hours.py # open/close boundaries across DST
    test_market_mid.py # TradingView webhook, market-mid status, vs-mid on quotes
    test_api.py      # HTTP routes via TestClient (skipped if fastapi/httpx missing)
```

## Commands

```bash
pip install -e ".[dev]"            # runtime deps + pytest + httpx
pytest                             # run tests (pyproject sets pythonpath=src)
python -m execution_cost_tracker.run --sizes 1000,10000 --no-store   # one live cycle
uvicorn app:app --reload           # local API; docs at /docs
```

Environment: `BASE_RPC_URL`, `ZEROX_API_KEY`, `CRON_SECRET`, `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `MAX_NOTIONAL`, `TRADINGVIEW_WEBHOOK_SECRET`. Secrets live in Vercel project settings or a local `.env`, never in code.

## Conventions

- **Cost sign:** a positive cost always means worse for the trader. Slippage is `side.sign * (price - benchmark_price) * quantity`, so paying up on a buy or selling low is positive; price improvement is negative. FX `deviation_bps` follows the same rule against the reference mid.
- **Basis points** are measured against benchmark or reference notional and multiplied by 10,000.
- **FX prices** are always quote units per one base unit (USDC per EURC), so they compare directly with the reference pair (EUR/USD).
- **Quote sides** are from the trader's view on the base token. A sell is exact-input of `size` base. A buy is exact-input of `size * mid` quote, so both sides trade about the same notional.
- `Execution`, `CostSummary`, `Token`, `Pair`, `Quote` and `ReferenceRate` are frozen dataclasses. Derived metrics are read-only properties, not stored fields.
- `Execution` validates in `__post_init__`: quantity and prices must be positive, and commission and fees non-negative. `side` accepts a `Side` or a string such as `"buy"`.
- `ExecutionCostTracker.filter()` returns a new tracker and never mutates the original. `start` is inclusive and `end` is exclusive.
- `metrics.py` stays pure: no network, clocks, or I/O.
- **Venues:** subclass `Venue` and implement `simulate()`. Try candidate pools with `best_of()`, and raise `VenueError` when nothing quotes. Register new venues in `venues/__init__.py`.
- A venue failure must never abort a cycle. `run_cycle` records it in `failures`.
- Contract addresses live as per-chain dicts at the top of each venue module, each with a source comment. Verify new addresses against official docs or Basescan before adding them.
- **Dependencies:** runtime deps (`fastapi`, `web3`, `turso_serverless`, `pyyaml`) are listed in `pyproject.toml` so Vercel installs them. Each is imported in exactly one place: `api.py`, `chain.py` (lazily), `storage.connect()` (only when Turso is configured), and `registry.load()`. Everything else uses the standard library only.
- **Registry:** chains, tokens, pairs and limits live in `config.yaml`, never hard-coded. Code gets them from `registry.get()`. Adding a token or pair is a config change. Adding a chain also needs venue contract addresses in the venue modules. Any new field gets validation in `registry.parse()` and a test in `test_registry.py`.
- **Front end:** `public/index.html` is one self-contained file that builds its form from `/api/config` and never holds keys. It calls only this app's `/api/...` endpoints. Keep it working with `side` = buy, sell or both, and with any number of venues.
- **Guardrails:** every public quote goes through `service.guarded_quote`, which checks the cache, then the per-client limit, then the global limit. Network calls happen outside the lock. Only results with at least one quote are cached.
- **Quote band:** `service.quote` drops quotes priced more than `quality.max_distance_from_mid_pct` (config.yaml, default 2%) from the reference rate. They go into `excluded` and are left out of `quotes`, `rows` and `best`. The check is stateless and runs on every request, so a venue comes back as soon as it's inside the band. Never persist exclusions. The scheduled run stores every quote unfiltered, so history keeps the outliers.
- **Market mid:** the page's "vs mkt mid" column uses the latest TradingView price stored by `/api/tradingview/webhook` (`market_mids` table). It is only valid while `market_hours.is_open(now)` holds and the price is younger than `market_mid.max_age_seconds`. Otherwise `vs_market_mid_bps` is `None` (shown as N/A) and `market_mid.reason` says why. TradingView has no data API; never scrape it. Alert messages have no `{{bid}}`/`{{ask}}` placeholders; bid and ask come from the Pine script in `tradingview/` (1T chart only), and the webhook stores their average plus both values. The webhook rejects bodies with unfilled `{{...}}` placeholders with an explanatory 400. Columns added to existing tables go in `storage._ADDED_COLUMNS` so `connect()` migrates old databases. The ECB daily reference stays the basis for the 2% band and stored `deviation_bps`. Market-hours code takes `now` as a parameter and never reads the clock itself.
- **Cross-chain:** `QuoteParams` accepts `source_chain`/`destination_chain` but rejects different values until bridge routes exist.
- **API layering:** keep `api.py` to HTTP mapping only. Validation and logic go in `service.py`, which must not import FastAPI so it stays testable offline.
- The `/api/cron` endpoint must stay closed unless `CRON_SECRET` is set and matches. Public inputs are validated against known pairs and venues and capped by `MAX_NOTIONAL`.
- Storage code goes through cursors and `conn.commit()`, never `sqlite3`-only features (`row_factory`, `executescript`, `with conn:`), so it works with both SQLite and Turso.
- Tests are offline. Inject fakes (`http_get=`, `w3=`, fake `Venue` subclasses) rather than hitting the network.
- If you add a field to `Execution`, update `_CSV_FIELDS`, `to_csv`, and `from_csv` in `tracker.py` together, and extend the CSV round-trip test. If you add a field to `Quote`, update the `quotes` table and `save_quote` in `storage.py`.
- Target Python 3.10+ and use type hints throughout.
- Every behaviour change comes with a test in `tests/`.
