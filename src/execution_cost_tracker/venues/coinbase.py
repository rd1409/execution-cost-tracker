"""Coinbase (Advanced Trade): effective price from walking the live order book.

Selling N EURC walks down the bids, buying walks up the asks, and the price
is the size-weighted average of the levels filled (see ``orderbook.py``).

The book comes from the Advanced Trade product book endpoint:
  * with COINBASE_API_KEY and COINBASE_API_SECRET set, via Coinbase's SDK
    (``coinbase-advanced-py``), GET /api/v3/brokerage/product_book;
  * otherwise from the public GET /api/v3/brokerage/market/product_book.
Docs: https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/products/get-product-book

Pair params (``venue_params["coinbase"]`` in config.yaml):
  product_id     Coinbase product, e.g. "EURC-USDC" (required; pairs without it skip this venue)
  depth_limit    how many bid/ask levels to request (default 500)
  taker_fee_bps  your Coinbase taker fee in bps, deducted from what you receive (default 0)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .. import orderbook
from ..models import Pair, Token
from ..reference import USER_AGENT
from .base import SwapResult, Venue, VenueError

PUBLIC_BOOK_URL = "https://api.coinbase.com/api/v3/brokerage/market/product_book"
DEFAULT_DEPTH = 500
BOOK_TTL_SECONDS = 2.0  # buy and sell quotes in one request share a single book

BookFetcher = Callable[[str, int], dict]


def _secret_from_env() -> str | None:
    secret = os.environ.get("COINBASE_API_SECRET")
    # PEM keys pasted into a single-line setting often arrive with literal "\n".
    return secret.replace("\\n", "\n") if secret else None


def fetch_book(product_id: str, limit: int) -> dict:
    """The product book as a dict with a "pricebook" key."""
    key, secret = os.environ.get("COINBASE_API_KEY"), _secret_from_env()
    if key and secret:
        try:
            from coinbase.rest import RESTClient  # third-party; only needed here
        except ImportError as e:
            raise VenueError("coinbase: install coinbase-advanced-py to use COINBASE_API_KEY") from e
        try:
            resp = RESTClient(api_key=key, api_secret=secret).get_product_book(product_id=product_id, limit=limit)
        except Exception as e:
            raise VenueError(f"coinbase: product book request failed: {type(e).__name__}: {e}"[:300]) from e
        return resp.to_dict() if hasattr(resp, "to_dict") else dict(resp)
    return _fetch_public(product_id, limit)


def _fetch_public(product_id: str, limit: int) -> dict:
    url = f"{PUBLIC_BOOK_URL}?{urllib.parse.urlencode({'product_id': product_id, 'limit': limit})}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        raise VenueError(f"coinbase: HTTP {e.code} for {product_id}: {body}") from e


class CoinbaseOrderBook(Venue):
    name = "coinbase"

    def __init__(self, fetcher: BookFetcher | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch = fetcher or fetch_book
        self._clock = clock
        self._cache: dict[tuple, tuple[float, dict]] = {}

    def supports(self, pair: Pair) -> bool:
        return bool(pair.params_for(self.name).get("product_id"))

    def _book(self, product_id: str, limit: int) -> dict:
        key = (product_id, limit)
        hit = self._cache.get(key)
        if hit and self._clock() - hit[0] < BOOK_TTL_SECONDS:
            return hit[1]
        book = self._fetch(product_id, limit)
        self._cache[key] = (self._clock(), book)
        return book

    def simulate(self, pair: Pair, token_in: Token, token_out: Token, amount_in: int) -> SwapResult:
        params = pair.params_for(self.name)
        product_id = params.get("product_id")
        if not product_id:
            raise VenueError(f"coinbase: no product_id configured for {pair.name}")
        limit = int(params.get("depth_limit", DEFAULT_DEPTH))
        fee_bps = float(params.get("taker_fee_bps", 0))

        data: Any = self._book(product_id, limit)
        book = (data or {}).get("pricebook") or {}
        selling = token_in.address.lower() == pair.base.address.lower()
        levels = orderbook.parse_levels(book.get("bids" if selling else "asks") or [], "bids" if selling else "asks")
        if not levels:
            raise VenueError(f"coinbase: {product_id} order book has no {'bids' if selling else 'asks'}")

        amount = token_in.from_raw(amount_in)
        fill = orderbook.sell_base(levels, amount) if selling else orderbook.buy_with_quote(levels, amount)
        if not fill.complete:
            have = fill.base_filled if selling else fill.quote_amount
            unit = pair.base.symbol if selling else pair.quote.symbol
            raise VenueError(
                f"coinbase: the {limit} best {'bids' if selling else 'asks'} only cover {have:,.0f} {unit} "
                f"of the {amount:,.0f} requested (raise depth_limit or trade smaller)"
            )

        base_out, quote_out = orderbook.apply_fee(fill, fee_bps)
        amount_out = quote_out if selling else base_out
        return SwapResult(
            amount_out=token_out.to_raw(amount_out),
            gas_estimate=None,
            meta={
                "pool": f"order book, {fill.levels_used} level{'s' if fill.levels_used != 1 else ''}",
                "product_id": product_id,
                "levels_used": fill.levels_used,
                "top_of_book": fill.best_price,
                "worst_price": fill.worst_price,
                "avg_price_before_fee": fill.avg_price,
                "slippage_vs_top_bps": round(fill.slippage_vs_best_bps, 4),
                "taker_fee_bps": fee_bps,
                "book_time": book.get("time"),
            },
        )
