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

The package also tracks tokenised FX: it quotes EURC/USDC on Base from Uniswap v3, Uniswap v4, Aerodrome and the 0x aggregator, plus Coinbase's EURC-USDC order book. It then compares each quote with the EUR/USD reference mid and stores the results in SQLite.

```bash
pip install -e ".[dev]"
export BASE_RPC_URL=https://...      # optional; defaults to the public Base RPC
export ZEROX_API_KEY=...             # optional; needed for the 0x venue
export COINBASE_API_KEY=... COINBASE_API_SECRET=...   # optional; Coinbase book (public endpoint otherwise)
python -m execution_cost_tracker.run --sizes 1000,10000,100000
```

Each cycle prints the following for every venue and size:

- bid and ask in USDC per EURC
- the cost of selling and of buying, in bps from the reference mid (positive means worse than mid)
- the round-trip spread in bps
- the venue mid's offset from the reference mid

Failed venues are listed with their errors and never stop the cycle. Everything is written to `fxtracker.db`; pass `--no-store` to skip writing.

The reference price is the latest EUR/USD price received from TradingView (see "Market mid from TradingView" below). It's used to size buy quotes, hide outliers, and centre the chart. Over a weekend that's Friday's last price. Until TradingView has sent anything, the app estimates a mid from the venues' own prices.

## Web API and Vercel

`app.py` at the repo root is the entrypoint Vercel looks for. It loads the FastAPI app in `src/execution_cost_tracker/api.py`:

| Endpoint | What it does |
|---|---|
| `GET /` | The front-end page (`public/index.html`) |
| `GET /api/health` | Liveness check |
| `GET /api/config` | Chains, tokens, pairs, venues and limits the front end builds its form from |
| `GET /api/pairs` | One entry per pair and chain, with token details |
| `POST /api/route` | What the page uses: prices a conversion end to end, from a start stablecoin and chain to an end stablecoin and chain, through every venue. Not stored; same rate limits and cache. |
| `POST /api/quote` | Single-chain quote for one pair, not stored. Rate limited and briefly cached; see below. |
| `POST /api/tradingview/webhook` | TradingView alert pushes the live EUR/USD price. Secret in the message; TradingView's IPs only. |
| `GET /api/cron` | Scheduled run: quotes every pair, venue and size, then stores everything. Requires `Authorization: Bearer <CRON_SECRET>`. |

An example `POST /api/route` body (every field is optional):

```json
{"from_token": "USDC", "from_chain": "tempo", "to_token": "EURC", "to_chain": "solana",
 "amount": 50000, "venues": ["coinbase", "uniswap_v3", "aerodrome"]}
```

See "Routes" below for what comes back. An example `POST /api/quote` body (every field is optional):

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

TradingView has no API for pulling prices out of an account. Instead, TradingView **pushes** the price to this app about once a minute through a webhook. There are two ways to set it up. Both start with:

1. **Turn on two-factor authentication** in your TradingView account settings. TradingView only sends webhooks from accounts with 2FA on. If the Webhook URL option is unavailable when you create the alert, your TradingView plan doesn't include webhooks.
2. **Add a secret in Vercel.** Under Settings → Environment Variables, add `TRADINGVIEW_WEBHOOK_SECRET` with a long random string, for example from `python3 -c "import secrets; print(secrets.token_hex(32))"`. Then redeploy.

**Option A (recommended): true mid from bid and ask, using the Pine script.** TradingView alert messages have **no `{{bid}}` or `{{ask}}` placeholders**. If you type them, TradingView sends them unfilled and the app rejects the message with an explanation. Pine scripts *can* read `bid` and `ask`, but only on the 1-tick chart, so `tradingview/fx_tracker_bid_ask.pine` builds the message itself:

3. In TradingView, open the **Pine Editor** (bottom panel), paste the contents of `tradingview/fx_tracker_bid_ask.pine`, **Save**, then **Add to chart** on EUR/USD.
4. Pick the timeframe. On a **1-tick ("1T")** chart the script sends `bid` and `ask`. On any other chart (1 second, 1 minute, …) `bid` and `ask` are empty, so it sends the chart's latest price instead. That's the same as Option B, but via the script. Tick charts aren't on every TradingView plan.
5. Open the script's **Settings** (gear icon on its label) and paste your secret from step 2. Keep the script private; don't publish it.
6. **Create an alert:** Condition = *FX tracker: bid/ask webhook* → **Any alert() function call**. Expiration: the longest allowed. Notifications: tick **Webhook URL** and enter `https://YOUR-APP.vercel.app/api/tradingview/webhook`. Leave the Message box alone; the script supplies the message.

The script sends at most once a minute (adjustable in its settings). The page headline then shows "market mid from TradingView, average of bid … and ask …".

**Option B (simpler): the chart price.** On a 1-minute EUR/USD chart, create an alert with Condition = EURUSD **Greater Than** `0`, Trigger = **Once Per Bar Close**, Webhook URL as above, and this Message (with your secret):

```json
{"secret": "YOUR_SECRET", "ticker": "{{ticker}}", "price": {{close}}, "time": "{{timenow}}"}
```

`{{close}}` is the chart's latest price. Depending on the feed, that may be a mid or a bid; see "how to check" below.

**Check it's working:** during market hours, wait a minute or two and run a quote. TradingView's alert log has a "Webhook status" column if anything fails.

Notes:
- The endpoint only accepts requests from TradingView's published webhook addresses (listed in `config.yaml`) and checks the secret in the message. Wrong secret → 401; wrong address → 403.
- The webhook accepts either `"price"` or `"bid"` plus `"ask"`; with bid and ask it stores their average as the mid and keeps both for display.
- Option B only: to see what your chart's price is, turn on Bid and Ask lines in Chart settings. If the price sits on the bid line it's a bid; if it's halfway between the lines it's a mid. A bid instead of a mid puts "vs mkt mid" roughly 0.1–0.5 bps off on EUR/USD.
- The Pine script hasn't been tested on a live TradingView account yet. If TradingView reports an error when you save or add it, note the message and line number; the script is short and its comments explain each part.
- Outside FX hours, the latest TradingView price (e.g. Friday's close) is still used behind the scenes to size buy quotes and for the 2% hidden-quotes filter, and the page shows it as "last TradingView price".
- Settings live under `market_mid` in `config.yaml`: how old a price can be, which TradingView ticker maps to which pair, and the IP allowlist.

### Coinbase order book

The `coinbase` venue prices a trade by walking Coinbase's EURC-USDC order book, as if you sent a market order:

- **Sell N EURC:** take the best bid, fill as much as it holds, move to the next bid, and repeat until N EURC is filled. The price is the size-weighted average: total USDC received / N. For example, selling 1,000,000 EURC into bids of 300k @ 1.1230, 500k @ 1.1228 and 400k @ 1.1225 fills 300k + 500k + 200k, for an average of 1.12279.
- **Buy:** the same walk up the asks, spending the USDC amount (size x mid), like the on-chain venues.

If the book's depth can't fill the size, the venue reports a failure saying how much it could fill. The page's route tooltip shows how many levels were used; the API's `meta` also has the top-of-book price, the worst level hit and the slippage from top of book. The pure walk lives in `orderbook.py`.

Settings under `venue_params.coinbase` for the pair in `config.yaml`:

- `product_id`: the Coinbase product (`EURC-USDC`). If Coinbase rejects it, try `EURC-USD`.
- `depth_limit`: how many levels to request (default 500). Raise it if large sizes run out of depth.
- `taker_fee_bps`: your Coinbase taker fee, deducted from what you receive. It's 0 by default, so prices are before fees.

With `COINBASE_API_KEY` and `COINBASE_API_SECRET` set, the book comes through Coinbase's SDK (`coinbase-advanced-py`). Without them it uses Coinbase's public product-book endpoint. A secret pasted on one line with literal `\n` is accepted.

### Routes: start and end on any chain

The page asks for the stablecoin you start with and its chain, and the stablecoin you want and the chain it should arrive on. Chains: Ethereum, Solana, Base, Arbitrum, Polygon and Tempo. For every venue that trades the pair, `routes.py` builds and prices a route in three steps:

1. **Wallet to venue.**
   - **DEX on the start chain:** nothing to move.
   - **DEX on another chain:** a bridge. Uniswap trades on Base and Ethereum, Aerodrome on Base, and 0x on Base and Ethereum.
   - **Coinbase:** a plain send to your deposit address if Coinbase takes the coin on that chain. Otherwise a bridge to a Coinbase network, with your deposit address as the bridge's recipient.
2. **The trade.**
   - **DEXes:** the existing quoters.
   - **Coinbase:** a walk of the EURC-USDC order book as a taker, including the taker fee.
3. **Venue to destination.** The reverse of step 1. Coinbase withdraws directly if it sends the coin on the end chain. Otherwise it withdraws to a Coinbase network and bridges from there.

**What each bridge step does:**
- **Quotes:** it asks Across, Relay and LayerZero (Stargate), and the one that delivers the most after gas wins. The other quotes and errors are listed under the route's steps.
- **Gas:** each step you sign is costed in its chain's gas token: ETH on Ethereum, Base and Arbitrum, POL on Polygon, SOL on Solana, and a USD stablecoin on Tempo. It uses the chain's current gas price (`eth_gasPrice`), valued with Coinbase's ETH-USD, POL-USD and SOL-USD prices.
- **Gas tokens needed:** the page lists, per route, which gas tokens on which chains you need. Steps Coinbase performs need none.

**Coinbase rules, all in `config.yaml` under `routing.coinbase`:**
- **USDC** deposits and withdrawals run directly on Ethereum, Solana, Base, Arbitrum and Polygon.
- **EURC** runs directly on Ethereum, Solana and Base.
- **Bridge hubs:** anything else bridges via one of `bridge_hubs` (Base, Arbitrum, Ethereum).
- **Tempo USDC:** Coinbase now also lists Tempo as a USDC network. It's left out to match the requirement to bridge Tempo routes. Add `tempo` to `networks.USDC` to deposit and withdraw directly.
- **Send fees:** Coinbase charges its estimate of the network fee on sends. That's estimated from live gas for a token transfer unless you set a fixed fee in `withdrawal_fees`. Deposits are free.

**Taker fee:** `venue_params.coinbase.taker_fee_bps` is 0.45 bps. That's Coinbase Exchange's published stable-pair taker rate for standard users (EURC-USDC is a stable pair). Coinbase only shows the Advanced Trade retail stable-pair rate on your logged-in fee page (Advanced, then Fees, then Stablecoin pairs). Put your rate there. EURC-USDC is currently `limit_only` on Coinbase, so a real taker sends a marketable limit order. The cost is the same as the order-book walk.

**Known gaps** (the page says so per route instead of guessing):
- Circle doesn't issue EURC on Arbitrum or Polygon, so those chains can't be picked for EURC.
- Across and Relay don't list EURC routes, and LayerZero's API doesn't support EURC.e on Tempo. Any EURC route that needs a bridge therefore shows as "couldn't be priced". That covers Tempo EURC, and EURC between Base, Ethereum and Solana. To have a bridge asked about EURC, add EURC to its `tokens` under `routing.bridges`.
- On Tempo, EURC.e can't pay fees. You need some USDC.e or pathUSD there for gas.
- Gas amounts per step are typical figures (`routing.gas_units`). Uniswap, 0x and Relay give their own figures, which are used instead.

**Keys, set in Vercel's environment variables:**
- `RELAY_API_KEY` (from dashboard.relay.link): Relay requires one from 2 October 2026.
- `LAYERZERO_API_KEY`: needed for LayerZero quotes. Without it, LayerZero shows "set LAYERZERO_API_KEY" and the other bridges are used.
- `ACROSS_API_KEY`: optional.
- RPC URLs: optional `ETHEREUM_RPC_URL`, `ARBITRUM_RPC_URL`, `POLYGON_RPC_URL`, `TEMPO_RPC_URL` and `BASE_RPC_URL`. Without them, the public RPCs listed in `config.yaml` are used.

### Hidden quotes: the 2% band

Quotes priced more than 2% above or below the latest market price are left out of the page's table, chart and "best" picks, and listed under a "hidden" note instead. They usually come from pools too thin for the size requested. The check runs fresh on every request, so a venue reappears as soon as its price is back within 2%. The API returns hidden quotes in `excluded`, with `offset_pct` showing how far off they were. Change the threshold with `quality.max_distance_from_mid_pct` in `config.yaml`. The scheduled run still stores every quote, so the history includes outliers.

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
   - `RELAY_API_KEY`, `LAYERZERO_API_KEY` (and optionally `ACROSS_API_KEY`): bridge quotes for routes (see "Routes")
   - `ETHEREUM_RPC_URL`, `ARBITRUM_RPC_URL`, `POLYGON_RPC_URL`, `TEMPO_RPC_URL`: optional provider RPCs for gas prices and Ethereum quotes
   - `COINBASE_API_KEY` and `COINBASE_API_SECRET`: optional, a Coinbase Developer Platform key with view access (see "Coinbase order book")
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
