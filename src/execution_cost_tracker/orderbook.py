"""Walk an order book to find the effective price of a trade.

Selling base (e.g. 1,000,000 EURC into USDC) hits the bids from the highest
price down; buying lifts the asks from the lowest price up. Each level is
filled in full until the remaining amount fits inside one level, which is
filled partially. The effective price is the size-weighted average:

    avg price = total quote (USDC) / total base (EURC)

Pure functions: no network, clocks or I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping


@dataclass(frozen=True)
class Level:
    price: float  # quote per base, e.g. USDC per EURC
    size: float   # base units available at that price


@dataclass(frozen=True)
class BookFill:
    """The result of walking one side of the book.

    ``base_filled`` and ``quote_amount`` are what actually filled. If the book
    ran out first, ``complete`` is False and they cover everything available.
    """

    side: str             # "sell" (hit bids) or "buy" (lift asks)
    requested: float      # what was asked for, in base or quote units (see ``requested_in``)
    requested_in: str     # "base" or "quote"
    base_filled: float
    quote_amount: float
    levels_used: int
    best_price: float
    worst_price: float
    complete: bool

    @property
    def avg_price(self) -> float:
        """Size-weighted average price (quote per base)."""
        return self.quote_amount / self.base_filled

    @property
    def slippage_vs_best_bps(self) -> float:
        """How much worse the average is than the top of book, in bps (>= 0)."""
        sign = 1 if self.side == "buy" else -1
        return sign * (self.avg_price - self.best_price) / self.best_price * 10_000


def parse_levels(raw: Iterable[Mapping | tuple | list], side: str) -> list[Level]:
    """Turn API levels (``{"price": "1.12", "size": "500"}`` or ``[price, size]``)
    into Levels sorted best-first: bids high to low, asks low to high.
    Skips levels with a non-positive or non-numeric price or size."""
    if side not in ("bids", "asks"):
        raise ValueError("side must be 'bids' or 'asks'")
    levels = []
    for item in raw:
        try:
            if isinstance(item, Mapping):
                price, size = float(item["price"]), float(item["size"])
            else:
                price, size = float(item[0]), float(item[1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if math.isfinite(price) and math.isfinite(size) and price > 0 and size > 0:
            levels.append(Level(price, size))
    levels.sort(key=lambda lv: lv.price, reverse=(side == "bids"))
    return levels


def _walk(levels: list[Level], side: str, amount: float, amount_in: str) -> BookFill:
    if not (math.isfinite(amount) and amount > 0):
        raise ValueError("amount must be a positive number")
    if not levels:
        raise ValueError(f"no {'bids' if side == 'sell' else 'asks'} in the order book")

    remaining = amount
    base = quote = 0.0
    used = 0
    worst = levels[0].price
    for lv in levels:
        if remaining <= 0:
            break
        # How much of this level do we take, in base units?
        if amount_in == "base":
            take = min(lv.size, remaining)
            remaining -= take
        else:  # spending a fixed amount of quote
            level_cost = lv.size * lv.price
            take = lv.size if level_cost <= remaining else remaining / lv.price
            remaining -= take * lv.price
        base += take
        quote += take * lv.price
        used += 1
        worst = lv.price

    # Allow for floating-point dust when the last level exactly finishes the order.
    complete = remaining <= amount * 1e-12
    return BookFill(
        side=side,
        requested=amount,
        requested_in=amount_in,
        base_filled=base,
        quote_amount=quote,
        levels_used=used,
        best_price=levels[0].price,
        worst_price=worst,
        complete=complete,
    )


def sell_base(bids: list[Level], base_amount: float) -> BookFill:
    """Sell exactly ``base_amount`` (e.g. EURC) into the bids."""
    return _walk(bids, "sell", base_amount, "base")


def buy_base(asks: list[Level], base_amount: float) -> BookFill:
    """Buy exactly ``base_amount`` (e.g. EURC) from the asks."""
    return _walk(asks, "buy", base_amount, "base")


def buy_with_quote(asks: list[Level], quote_amount: float) -> BookFill:
    """Spend exactly ``quote_amount`` (e.g. USDC) buying base from the asks."""
    return _walk(asks, "buy", quote_amount, "quote")


def apply_fee(fill: BookFill, fee_bps: float) -> tuple[float, float]:
    """(base, quote) the trader ends up with after a taker fee in bps.

    Selling: the fee comes off the quote received. Buying: off the base received.
    """
    keep = 1 - fee_bps / 10_000
    if fill.side == "sell":
        return fill.base_filled, fill.quote_amount * keep
    return fill.base_filled * keep, fill.quote_amount
