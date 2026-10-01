"""The shared interface every venue implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Iterable

from ..models import Pair, Quote, Side, Token


class VenueError(Exception):
    """A venue could not produce a quote (no pool, no liquidity, RPC error...)."""


@dataclass
class SwapResult:
    """Raw output of an exact-input swap simulation."""

    amount_out: int
    gas_estimate: int | None = None
    meta: dict = field(default_factory=dict)


class Venue(ABC):
    """A place that can quote an exact-input swap between two tokens.

    Subclasses implement :meth:`simulate`. The base class turns that into a
    :class:`Quote` priced in quote-per-base units:

    * sell ``size`` base  -> exact input of ``size`` base tokens
    * buy  ``size`` base  -> exact input of ``size * mid_hint`` quote tokens,
      so both sides trade roughly the same notional.
    """

    name: str = "venue"

    @abstractmethod
    def simulate(self, pair: Pair, token_in: Token, token_out: Token, amount_in: int) -> SwapResult:
        """Simulate swapping ``amount_in`` raw units of ``token_in``."""

    def supports(self, pair: Pair) -> bool:
        """Whether this venue can quote ``pair`` at all. Override to opt out."""
        return True

    def quote(self, pair: Pair, side: Side | str, size: float, mid_hint: float) -> Quote:
        side = side if isinstance(side, Side) else Side(str(side).lower())
        if size <= 0 or mid_hint <= 0:
            raise ValueError("size and mid_hint must be positive")

        if side is Side.SELL:
            token_in, token_out, amount_in = pair.base, pair.quote, size
        else:
            token_in, token_out, amount_in = pair.quote, pair.base, size * mid_hint

        result = self.simulate(pair, token_in, token_out, token_in.to_raw(amount_in))
        if result.amount_out <= 0:
            raise VenueError(f"{self.name}: zero output for {pair.name} {side.value}")
        amount_out = token_out.from_raw(result.amount_out)

        base_amount, quote_amount = (amount_in, amount_out) if side is Side.SELL else (amount_out, amount_in)
        return Quote(
            venue=self.name,
            pair=pair.name,
            chain=pair.chain,
            side=side,
            base_amount=base_amount,
            quote_amount=quote_amount,
            gas_estimate=result.gas_estimate,
            meta=result.meta,
        )


def best_of(attempts: Iterable[tuple[str, Callable[[], SwapResult]]], venue: str) -> SwapResult:
    """Run several pool attempts and keep the one with the largest output.

    Each attempt is ``(label, fn)``. Failures are collected; if every attempt
    fails a :class:`VenueError` lists why.
    """
    best: SwapResult | None = None
    errors: list[str] = []
    for label, fn in attempts:
        try:
            result = fn()
        except Exception as e:  # each pool may revert or not exist
            errors.append(f"{label}: {type(e).__name__}: {e}"[:200])
            continue
        result.meta.setdefault("pool", label)
        if result.amount_out > 0 and (best is None or result.amount_out > best.amount_out):
            best = result
    if best is None:
        raise VenueError(f"{venue}: no pool returned a quote ({'; '.join(errors) or 'no attempts'})")
    return best
