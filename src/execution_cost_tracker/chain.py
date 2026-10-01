"""RPC connection setup and contract helpers (web3).

web3 is listed in pyproject dependencies but imported lazily, so modules that
don't touch the chain (metrics, storage, tests) work without it installed.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from importlib import resources
from typing import Any

# Public RPC defaults. They are rate limited; set e.g. BASE_RPC_URL to a
# provider endpoint (Alchemy, Infura, QuickNode, ...) for regular use.
DEFAULT_RPC_URLS = {
    "base": "https://mainnet.base.org",
}

CHAIN_IDS = {
    "base": 8453,
}


def rpc_url_for(chain: str) -> str:
    """RPC URL for a chain: ``<CHAIN>_RPC_URL`` env var, else the default."""
    env = os.environ.get(f"{chain.upper()}_RPC_URL")
    if env:
        return env
    try:
        return DEFAULT_RPC_URLS[chain]
    except KeyError:
        raise ValueError(f"No RPC URL for chain {chain!r}; set {chain.upper()}_RPC_URL") from None


def _web3_module():
    try:
        import web3  # noqa: F401
    except ImportError as e:  # pragma: no cover - depends on environment
        raise ImportError(
            "web3 is required for on-chain venues. Install with: pip install -e ."
        ) from e
    return web3


@lru_cache(maxsize=None)
def get_web3(chain: str = "base", rpc_url: str | None = None) -> Any:
    """A connected Web3 instance for ``chain``. Cached per (chain, url)."""
    web3 = _web3_module()
    url = rpc_url or rpc_url_for(chain)
    w3 = web3.Web3(web3.Web3.HTTPProvider(url, request_kwargs={"timeout": 20}))
    expected = CHAIN_IDS.get(chain)
    if expected is not None:
        actual = w3.eth.chain_id
        if actual != expected:
            raise RuntimeError(f"RPC {url} is chain {actual}, expected {chain} ({expected})")
    return w3


@lru_cache(maxsize=None)
def load_abi(name: str) -> list:
    """Load ``abis/<name>.json`` shipped with the package."""
    text = resources.files(__package__).joinpath("abis", f"{name}.json").read_text()
    return json.loads(text)


def contract(w3: Any, address: str, abi_name: str) -> Any:
    """A web3 contract object for ``address`` using a bundled ABI."""
    return w3.eth.contract(address=w3.to_checksum_address(address), abi=load_abi(abi_name))
