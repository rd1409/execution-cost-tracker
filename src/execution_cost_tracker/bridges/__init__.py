"""Bridges that move a stablecoin between chains, for routing to and from venues."""

from .across import Across
from .base import Bridge, BridgeError, BridgeQuote
from .layerzero import LayerZero
from .relay import Relay

BRIDGES: dict[str, type[Bridge]] = {
    Across.name: Across,
    Relay.name: Relay,
    LayerZero.name: LayerZero,
}

__all__ = ["Across", "BRIDGES", "Bridge", "BridgeError", "BridgeQuote", "LayerZero", "Relay"]
