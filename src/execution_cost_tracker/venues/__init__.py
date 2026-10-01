"""Venues that quote tokenised FX pairs."""

from .aerodrome import Aerodrome
from .base import SwapResult, Venue, VenueError
from .uniswap_v3 import UniswapV3
from .uniswap_v4 import UniswapV4
from .zerox import ZeroX

VENUES: dict[str, type[Venue]] = {
    UniswapV3.name: UniswapV3,
    UniswapV4.name: UniswapV4,
    Aerodrome.name: Aerodrome,
    ZeroX.name: ZeroX,
}

__all__ = ["Aerodrome", "SwapResult", "UniswapV3", "UniswapV4", "VENUES", "Venue", "VenueError", "ZeroX"]
