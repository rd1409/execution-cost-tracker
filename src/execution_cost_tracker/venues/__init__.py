"""Venues that quote tokenised FX pairs."""

from .aerodrome import Aerodrome
from .base import SwapResult, Venue, VenueError
from .coinbase import CoinbaseOrderBook
from .uniswap_v3 import UniswapV3
from .uniswap_v4 import UniswapV4
from .zerox import ZeroX

VENUES: dict[str, type[Venue]] = {
    UniswapV3.name: UniswapV3,
    UniswapV4.name: UniswapV4,
    Aerodrome.name: Aerodrome,
    ZeroX.name: ZeroX,
    CoinbaseOrderBook.name: CoinbaseOrderBook,
}

__all__ = ["Aerodrome", "CoinbaseOrderBook", "SwapResult", "UniswapV3", "UniswapV4", "VENUES", "Venue", "VenueError", "ZeroX"]
