"""Aerodrome (Base): Slipstream concentrated-liquidity pools and classic
stable/volatile pools. Tries both and keeps the best quote."""

from __future__ import annotations

from typing import Any

from .. import chain as chainmod
from ..models import Pair, Token
from .base import SwapResult, Venue, best_of

# Labelled on Basescan as "Aerodrome: Router" and "Aerodrome: SlipStream Quoter".
ROUTER = {"base": "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"}
SLIPSTREAM_QUOTER = {"base": "0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0"}
# Default classic-pool factory passed in router routes.
POOL_FACTORY = {"base": "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"}

DEFAULT_TICK_SPACINGS = [1, 50, 100]
DEFAULT_CLASSIC = [True, False]  # stable, volatile


class Aerodrome(Venue):
    """Pair params (``venue_params["aerodrome"]``):
    ``tick_spacings``: Slipstream tick spacings to try (default 1, 50, 100).
    ``classic``: classic pool types to try, True=stable, False=volatile.
    """

    name = "aerodrome"

    def __init__(self, w3: Any = None) -> None:
        self._w3 = w3

    def supports(self, pair: Pair) -> bool:
        return pair.chain in ROUTER

    def simulate(self, pair: Pair, token_in: Token, token_out: Token, amount_in: int) -> SwapResult:
        w3 = self._w3 or chainmod.get_web3(pair.chain)
        params = pair.params_for(self.name)
        a_in = w3.to_checksum_address(token_in.address)
        a_out = w3.to_checksum_address(token_out.address)

        quoter = chainmod.contract(w3, SLIPSTREAM_QUOTER[pair.chain], "aerodrome_slipstream_quoter")
        router = chainmod.contract(w3, ROUTER[pair.chain], "aerodrome_router")
        factory = w3.to_checksum_address(POOL_FACTORY[pair.chain])

        def cl(ts: int):
            def run() -> SwapResult:
                amount_out, _sqrt, ticks, gas = quoter.functions.quoteExactInputSingle(
                    (a_in, a_out, amount_in, ts, 0)
                ).call()
                return SwapResult(amount_out, gas, {"kind": "slipstream", "tick_spacing": ts, "ticks_crossed": ticks})

            return f"slipstream ts={ts}", run

        def classic(stable: bool):
            def run() -> SwapResult:
                amounts = router.functions.getAmountsOut(amount_in, [(a_in, a_out, stable, factory)]).call()
                return SwapResult(amounts[-1], None, {"kind": "classic", "stable": stable})

            return f"classic {'stable' if stable else 'volatile'}", run

        attempts = [cl(ts) for ts in params.get("tick_spacings", DEFAULT_TICK_SPACINGS)]
        attempts += [classic(s) for s in params.get("classic", DEFAULT_CLASSIC)]
        return best_of(attempts, self.name)
