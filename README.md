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
| `GET /` | The front-end page (`public/index.html`) |
| `GET /api/health` | Liveness check |
| `GET /api/config` | Chains, tokens, pairs, venues and limits the front end builds its form from |
| `GET /api/pairs` | One entry per pair and chain, with token details |
| `POST /api/quote` | On-demand quote, not stored. Rate limited and briefly cached; see below. |
| `POST /api/tradingview/webhook` | TradingView alert pushes the live EUR/USD price. Secret in the message; TradingView's IPs only. |
| `GET /api/cron` | Scheduled run: quotes every pair, venue and size, then stores everything. Requires `Authorization: Bearer <CRON_SECRET>`. |

An example `POST /api/quote` body (every field is optional):

```json
{"pair": "EURC/USDC", "notional": 25000, "side": "both",
 "source_chain": "base", "destination_chain": "base",
 "venues": ["uniswap_v3", "aerodrome", "zerox"]}
```

`side` is `buy`, `sell` or `both`. `destination_chain` defaults to `source_chain`. Different source and destination chains are rejected for now, because cross-chain routes aren't built yet. `venues` defaults to every venue that works on the chain.

FastAPI also serves interactive docs at `/docs`, where you can try requests in the browser.

To run it locally:

```bash
pip install -e ".[dev]"
uvicorn app:app --reload        # then open http://127.0.0.1:8000 (page) or /docs
```

### The registry: `config.yaml`

`config.yaml` at the repo root lists the chains, tokens (address and decimals) and pairs. It also sets the size limits, guardrails and the sizes the scheduled run quotes. The API only accepts what's listed there, and the front end's dropdowns are built from it. To add a token or pair on Base, edit the file and push; no code changes are needed. The app checks the file on startup and refuses malformed addresses, unknown tokens or chains, and duplicate pairs.

### Market mid from TradingView ("vs mkt mid")

The "vs mkt mid (bps)" column compares each venue's price with the live EUR/USD mid while the interbank FX market is open, from **Monday 05:00 Sydney to Friday 17:00 New York**. Outside those hours, or if the latest price is more than 3 minutes old, it shows **N/A**, and hovering over a cell shows why. Positive means worse than mid.

TradingView has no API for pulling prices out of an account. Instead, a TradingView alert **pushes** the price to this app every minute through a webhook. To set it up:

1. **Turn on two-factor authentication** in your TradingView account settings. TradingView only sends webhooks from accounts with 2FA on. If the Webhook URL option is unavailable when you create the alert, your TradingView plan doesn't include webhooks.
2. **Add a secret in Vercel.** Under Settings → Environment Variables, add `TRADINGVIEW_WEBHOOK_SECRET` with a long random string, for example from `python3 -c "import secrets; print(secrets.token_hex(32))"`. Then redeploy.
3. **Open a EUR/USD chart** in TradingView and set the timeframe to **1 minute**.
4. **Create an alert** (the alarm-clock icon):
   - Condition: EURUSD, **Greater Than**, value `0`. This is always true, so the alert fires on every bar.
   - Trigger: **Once Per Bar Close**, which means once a minute on a 1-minute chart.
   - Expiration: the longest your plan allows. Re-create the alert when it expires.
   - Notifications: tick **Webhook URL** and enter `https://YOUR-APP.vercel.app/api/tradingview/webhook`.
   - Message: replace `YOUR_SECRET` with the value from step 2:
     ```json
     {"secret": "YOUR_SECRET", "ticker": "{{ticker}}", "price": {{close}}, "time": "{{timenow}}"}
     ```
5. **Check it's working.** After a minute or two during market hours, run a quote. The headline should read "EUR/USD market mid from TradingView, 30s ago". TradingView's alert log has a "Webhook status" column if anything fails.

Notes:
- The endpoint only accepts requests from TradingView's published webhook addresses (listed in `config.yaml`) and checks the secret in the message. Wrong secret → 401; wrong address → 403.
- If you'd rather send a true bid/ask midpoint (e.g. from a Pine script), send `"bid"` and `"ask"` instead of `"price"`; the app stores their average.
- Some FX charts plot bid prices rather than mid, which can put "vs mkt mid" a fraction of a basis point off. Pick the EUR/USD feed you trust.
- The ECB daily reference rate is still used for the 2% hidden-quotes filter, and is shown when no market mid is available.
- Settings live under `market_mid` in `config.yaml`: how old a price can be, which TradingView ticker maps to which pair, and the IP allowlist.

### Hidden quotes: the 2% band

Quotes priced more than 2% above or below the reference rate are left out of the page's table, chart and "best" picks, and listed under a "hidden" note instead. They usually come from pools too thin for the size requested. The check runs fresh on every request, so a venue reappears as soon as its price is back within 2%. The API returns hidden quotes in `excluded`, with `offset_pct` showing how far off they were. Change the threshold with `quality.max_distance_from_mid_pct` in `config.yaml`. The scheduled run still stores every quote, so the history includes outliers.

### Guardrails on `/api/quote`

Because the page is public, each quote request costs RPC calls and 0x API calls:

- **Size limits:** `limits.min_notional` / `limits.max_notional` in `config.yaml`. The `MAX_NOTIONAL` env var overrides the maximum.
- **Known inputs only:** pairs, chains and venues must be in the registry, and the venue must work on that chain.
- **Rate limits:** `guardrails.per_client_per_minute` (per visitor IP) and `guardrails.global_per_minute`. Over the limit, the API returns HTTP 429 with a `Retry-After` header.
- **Short cache:** an identical request within `guardrails.cache_seconds` gets the previous answer without new RPC calls, and doesn't count against the limits. Failed results aren't cached.

The rate limits and cache live in memory, so on Vercel they apply per running instance. They stop casual abuse. For hard limits, add a rate-limit rule in Vercel's Firewall or a shared store such as Redis.

To deploy:

1. In Vercel, choose **Add New → Project** and import this GitHub repo. Each push to `main` then redeploys.
2. Under **Project → Settings → Environment Variables**, set:
   - `BASE_RPC_URL`: your Base RPC provider URL
   - `ZEROX_API_KEY`: from dashboard.0x.org
   - `CRON_SECRET`: a random string of 16+ characters
   - `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN`: from a Turso database
   - `MAX_NOTIONAL`: optional cap on quote size (default 1,000,000)
   - `TRADINGVIEW_WEBHOOK_SECRET`: shared secret for the TradingView market-mid webhook (see below)
3. `vercel.json` schedules `/api/cron` daily at 12:00 UTC. That is the most often the Hobby plan allows. On Pro, change the `schedule` (for example `*/15 * * * *`) and push.

Storage uses Turso when `TURSO_DATABASE_URL` is set and a local `fxtracker.db` otherwise. On Vercel it refuses to fall back to a local file, because files there don't persist.

## Tests

```bash
pytest
```
