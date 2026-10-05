"""The shared interface every bridge client implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..models import Token
from ..registry import Chain, Registry


class BridgeError(Exception):
    """A bridge couldn't quote a transfer (no route, amount too small, API error...)."""


@dataclass
class BridgeQuote:
    """One quoted transfer of a stablecoin from one chain to another.

    ``amount_out`` is what arrives, after any fee the bridge takes from the
    token itself. ``fee_usd`` is anything charged on top of that (e.g. a
    LayerZero messaging fee paid in the gas token). ``origin_gas_usd`` is the
    bridge's own estimate of the source-chain transaction fee, when its API
    gives one; otherwise the planner estimates it from ``gas_units``.
    """

    provider: str
    token: str
    from_chain: str
    to_chain: str
    amount_in: float
    amount_out: float
    fee_usd: float = 0.0
    origin_gas_usd: float | None = None
    gas_units: int | None = None
    eta_seconds: float | None = None
    route: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def token_fee(self) -> float:
        return self.amount_in - self.amount_out


class Bridge(ABC):
    name: str = "bridge"
    label: str = "Bridge"

    def covers(self, reg: Registry, token: str, from_chain: str, to_chain: str) -> bool:
        """Whether config.yaml says to ask this bridge about the transfer."""
        return reg.routing.bridge_covers(self.name, token, from_chain, to_chain)

    @abstractmethod
    def quote(self, reg: Registry, token: str, from_chain: str, to_chain: str, amount: float) -> BridgeQuote:
        """Quote moving ``amount`` of ``token`` from ``from_chain`` to ``to_chain``."""


def endpoints(reg: Registry, token: str, from_chain: str, to_chain: str) -> tuple[Chain, Token, Chain, Token]:
    """The two chains and the token on each, or BridgeError if either is missing."""
    a, b = reg.chains.get(from_chain), reg.chains.get(to_chain)
    if a is None or b is None:
        raise BridgeError(f"unknown chain {from_chain if a is None else to_chain!r}")
    ta, tb = a.tokens.get(token), b.tokens.get(token)
    if ta is None or tb is None:
        missing = a if ta is None else b
        raise BridgeError(f"{token} isn't listed on {missing.name}")
    return a, ta, b, tb


def quote_wallet(reg: Registry, chain: Chain) -> str:
    """Placeholder wallet for quote requests (never used to send anything)."""
    wallets = reg.routing.quote_wallets
    if chain.kind == "solana":
        return wallets.get("solana") or "So11111111111111111111111111111111111111112"
    return wallets.get("evm") or "0x03508bb71268bba25ecacc8f620e01866650532c"
