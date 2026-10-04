"""Spread and basis-point math for FX quotes.

Pure functions only: no network, no clocks, no I/O. Every cost-like number
follows the project's sign convention: positive means worse for the trader.
"""

from __future__ import annotations

from typing import Iterable

from .models import Quote, Side

BPS = 10_000


def bps(numerator: float, denominator: float) -> float:
    """numerator / denominator, in basis points."""
    if denominator == 0:
        raise ValueError("denominator must be non-zero")
    return numerator / denominator * BPS


def deviation_bps(price: float, mid: float, side: Side | str) -> float:
    """How far an executable price is from the reference mid, in bps.

    Buying base above mid, or selling base below mid, is a positive cost.
    """
    side = side if isinstance(side, Side) else Side(str(side).lower())
    return side.sign * bps(price - mid, mid)


def quote_deviation_bps(quote: Quote, mid: float) -> float:
    """:func:`deviation_bps` for a :class:`Quote`."""
    return deviation_bps(quote.price, mid, quote.side)


def quoted_spread_bps(bid: float, ask: float, mid: float | None = None) -> float:
    """Round-trip spread, in bps of ``mid`` (defaults to the bid/ask midpoint).

    ``bid`` is the price you can sell base at, ``ask`` the price you can buy at.
    """
    if mid is None:
        mid = midpoint(bid, ask)
    return bps(ask - bid, mid)


def midpoint(bid: float, ask: float) -> float:
    return (bid + ask) / 2


def mid_offset_bps(venue_mid: float, reference_mid: float) -> float:
    """Signed gap between a venue's implied mid and the reference mid.

    Positive means the venue values base above the reference (a premium).
    """
    return bps(venue_mid - reference_mid, reference_mid)


def best_quote(quotes: Iterable[Quote]) -> Quote | None:
    """The best quote for the trader among quotes of the same side.

    Highest price wins for sells, lowest for buys. Returns None if empty.
    """
    quotes = list(quotes)
    if not quotes:
        return None
    sides = {q.side for q in quotes}
    if len(sides) != 1:
        raise ValueError("best_quote needs quotes from a single side")
    side = sides.pop()
    return max(quotes, key=lambda q: q.price) if side is Side.SELL else min(quotes, key=lambda q: q.price)


def gas_cost_bps(gas_units: int, gas_price_wei: int, native_usd: float, notional_usd: float) -> float:
    """Gas cost of a swap as bps of its USD notional."""
    gas_usd = gas_units * gas_price_wei / 1e18 * native_usd
    return bps(gas_usd, notional_usd)


def offset_pct(price: float, mid: float) -> float:
    """Signed distance of ``price`` from ``mid``, in percent (+ above, - below)."""
    if mid == 0:
        raise ValueError("mid must be non-zero")
    return (price - mid) / mid * 100


def within_band(price: float, mid: float, max_pct: float) -> bool:
    """True if ``price`` is no more than ``max_pct`` percent from ``mid`` either way.

    Used to drop quotes from broken or nearly empty pools, which can sit far
    from the market. The check is stateless: a venue that comes back inside
    the band is included again on the next quote. The boundary counts as
    inside (a tiny tolerance absorbs floating-point error, e.g. 2.0000000000000018).
    """
    return abs(offset_pct(price, mid)) <= max_pct + 1e-9
