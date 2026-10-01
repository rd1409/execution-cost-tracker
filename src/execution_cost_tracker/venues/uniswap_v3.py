"""Uniswap v3: quotes via QuoterV2.quoteExactInputSingle across fee tiers."""

from __future__ import annotations

from typing import Any

from .. import chain as chainmod
from ..models import Pair, Token
from .base import SwapResult, Venue, best_of

# https://developers.uniswap.org/docs/protocols/v3/deployments/v3-base-deployments
QUOTER_V2 = {
    "base": "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
}

DEFAULT_FEE_TIERS = [100, 500, 3000]


class UniswapV3(Venue):
    """Pair params (``venue_params["uniswap_v3"]``):
    ``fee_tiers``: fee tiers in hundredths of a bip to try (default 100, 500, 3000).
    """

    name = "uniswap_v3"

    def __init__(self, w3: Any = None) -> None:
        self._w3 = w3

    def supports(self, pair: Pair) -> bool:
        return pair.chain in QUOTER_V2

    def simulate(self, pair: Pair, token_in: Token, token_out: Token, amount_in: int) -> SwapResult:
        w3 = self._w3 or chainmod.get_web3(pair.chain)
        quoter = chainmod.contract(w3, QUOTER_V2[pair.chain], "uniswap_v3_quoter_v2")
        fee_tiers = pair.params_for(self.name).get("fee_tiers", DEFAULT_FEE_TIERS)

        def attempt(fee: int):
            def run() -> SwapResult:
                params = (
                    w3.to_checksum_address(token_in.address),
                    w3.to_checksum_address(token_out.address),
                    amount_in,
                    fee,
                    0,
                )
                amount_out, _sqrt_after, ticks_crossed, gas = quoter.functions.quoteExactInputSingle(params).call()
                return SwapResult(amount_out, gas, {"fee": fee, "ticks_crossed": ticks_crossed})

            return f"fee={fee}", run

        return best_of((attempt(f) for f in fee_tiers), self.name)
