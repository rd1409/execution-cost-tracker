"""Uniswap v4: quotes via V4Quoter.quoteExactInputSingle for candidate pool keys.

v4 pools live inside the singleton PoolManager and are identified by a pool
key (currency0, currency1, fee, tickSpacing, hooks), so we try a list of
candidate keys and keep the best quote.
"""

from __future__ import annotations

from typing import Any

from .. import chain as chainmod
from ..models import Pair, Token
from .base import SwapResult, Venue, best_of

# https://developers.uniswap.org/docs/protocols/v4/deployments
QUOTER = {
    "base": "0x0d5e0f971ed27fbff6c2837bf31316121532048d",
    "ethereum": "0x52f0e24d1c21c8a0cb1e5a5dd6198556bd9e1203",
}

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Standard fee/tick-spacing pairings, no hooks.
DEFAULT_POOLS = [
    {"fee": 100, "tick_spacing": 1},
    {"fee": 500, "tick_spacing": 10},
    {"fee": 3000, "tick_spacing": 60},
]


class UniswapV4(Venue):
    """Pair params (``venue_params["uniswap_v4"]``):
    ``pools``: list of ``{"fee", "tick_spacing", "hooks"?}`` pool keys to try.
    """

    name = "uniswap_v4"

    def __init__(self, w3: Any = None) -> None:
        self._w3 = w3

    def supports(self, pair: Pair) -> bool:
        return pair.chain in QUOTER

    def simulate(self, pair: Pair, token_in: Token, token_out: Token, amount_in: int) -> SwapResult:
        w3 = self._w3 or chainmod.get_web3(pair.chain)
        quoter = chainmod.contract(w3, QUOTER[pair.chain], "uniswap_v4_quoter")
        pools = pair.params_for(self.name).get("pools", DEFAULT_POOLS)

        a_in = w3.to_checksum_address(token_in.address)
        a_out = w3.to_checksum_address(token_out.address)
        # Pool keys order currencies by address.
        currency0, currency1 = sorted([a_in, a_out], key=lambda a: int(a, 16))
        zero_for_one = a_in == currency0

        def attempt(pool: dict):
            hooks = w3.to_checksum_address(pool.get("hooks", ZERO_ADDRESS))
            label = f"fee={pool['fee']},ts={pool['tick_spacing']}" + ("" if int(hooks, 16) == 0 else f",hooks={hooks}")

            def run() -> SwapResult:
                key = (currency0, currency1, pool["fee"], pool["tick_spacing"], hooks)
                params = (key, zero_for_one, amount_in, b"")
                amount_out, gas = quoter.functions.quoteExactInputSingle(params).call()
                return SwapResult(amount_out, gas, {"fee": pool["fee"], "tick_spacing": pool["tick_spacing"], "hooks": hooks})

            return label, run

        return best_of((attempt(p) for p in pools), self.name)
