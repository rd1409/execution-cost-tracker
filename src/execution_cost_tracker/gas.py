"""What a step costs in network fees, in the chain's gas token and in USD.

* EVM chains (and Tempo): ``gas units x eth_gasPrice`` from the chain's RPC.
* Solana: a flat fee per transaction (``routing.solana_fee_lamports``).

Gas tokens are valued in USD with Coinbase's public product price (e.g.
ETH-USD), or a fixed value from config.yaml (Tempo's fees are paid in USD
stablecoins, so 1). One oracle is created per request; it reads each chain's
gas price and each token's USD price at most once.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Callable

from . import httpjson
from .registry import Chain, Registry

COINBASE_PRODUCT_URL = "https://api.coinbase.com/api/v3/brokerage/market/products/"


class GasError(Exception):
    """A gas price or gas-token price couldn't be read."""


@dataclass(frozen=True)
class GasCost:
    chain: str
    gas_token: str
    units: int | None  # EVM gas units; None on Solana
    native: float | None  # amount of the gas token (None if only the USD figure is known)
    usd: float

    def as_dict(self) -> dict:
        return {"chain": self.chain, "gas_token": self.gas_token, "units": self.units, "native": self.native, "usd": self.usd}


class Memo:
    """Thread-safe compute-once cache: concurrent callers for the same key
    wait for the first one's result (or exception)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slots: dict = {}

    def get(self, key, fn: Callable[[], object]):
        with self._lock:
            slot = self._slots.get(key)
            owner = slot is None
            if owner:
                slot = self._slots[key] = {"done": threading.Event()}
        if owner:
            try:
                slot["value"] = fn()
            except Exception as e:  # remembered and re-raised to every caller
                slot["error"] = e
            finally:
                slot["done"].set()
        else:
            slot["done"].wait()
        if "error" in slot:
            raise slot["error"]
        return slot["value"]


def _rpc_url(chain: Chain) -> str:
    return os.environ.get(f"{chain.key.upper()}_RPC_URL") or chain.rpc_url or ""


class GasOracle:
    def __init__(
        self,
        reg: Registry,
        http_get: httpjson.HttpGet | None = None,
        http_post: httpjson.HttpPost | None = None,
    ) -> None:
        self.reg = reg
        self._get = http_get or (lambda url, headers: httpjson.get_json(url, headers, timeout=10))
        self._post = http_post or (lambda url, body, headers: httpjson.post_json(url, body, headers, timeout=10))
        self._memo = Memo()

    def gas_price_wei(self, chain_key: str) -> int:
        """Current gas price on an EVM chain, in the gas token's smallest unit."""
        chain = self.reg.chains[chain_key]

        def read() -> int:
            url = _rpc_url(chain)
            if not url:
                raise GasError(f"no RPC URL for {chain.name}; set {chain.key.upper()}_RPC_URL")
            try:
                data = self._post(url, {"jsonrpc": "2.0", "id": 1, "method": "eth_gasPrice", "params": []}, {})
                return int(data["result"], 16)
            except Exception as e:
                raise GasError(f"{chain.name} gas price unavailable ({type(e).__name__}: {e})"[:200]) from e

        return self._memo.get(("gas", chain_key), read)

    def gas_token_usd(self, chain_key: str) -> float:
        chain = self.reg.chains[chain_key]
        if chain.gas_token_usd is not None:
            return chain.gas_token_usd
        product = chain.gas_price_product
        if not product:
            raise GasError(f"no USD price source for {chain.gas_token}; set gas_price_product for {chain.key}")

        def read() -> float:
            try:
                data = self._get(COINBASE_PRODUCT_URL + product, {})
                price = float(data["price"])
            except Exception as e:
                raise GasError(f"{product} price unavailable ({type(e).__name__}: {e})"[:200]) from e
            if price <= 0:
                raise GasError(f"{product} price isn't positive")
            return price

        return self._memo.get(("usd", product), read)

    def cost(self, chain_key: str, units: int) -> GasCost:
        """Network fee for a transaction using ``units`` gas on ``chain_key``."""
        chain = self.reg.chains[chain_key]
        if chain.kind == "solana":
            native = self.reg.routing.solana_fee_lamports / 1e9
            return GasCost(chain_key, chain.gas_token, None, native, native * self.gas_token_usd(chain_key))
        native = units * self.gas_price_wei(chain_key) / 1e18
        return GasCost(chain_key, chain.gas_token, units, native, native * self.gas_token_usd(chain_key))
