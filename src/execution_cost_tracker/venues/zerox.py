"""0x Swap API (aggregator): indicative price via GET /swap/permit2/price.

Needs an API key from dashboard.0x.org in the ZEROX_API_KEY env var.
Docs: https://docs.0x.org/api-reference/evm-ap-is/swap/permit-2-getprice
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from ..models import Pair, Token
from ..reference import USER_AGENT
from .base import SwapResult, Venue, VenueError

PRICE_URL = "https://api.0x.org/swap/permit2/price"

HttpGet = Callable[[str, dict], dict]


def _http_get(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise VenueError(f"0x HTTP {e.code}: {body}") from e


class ZeroX(Venue):
    name = "zerox"

    def __init__(self, api_key: str | None = None, http_get: HttpGet | None = None) -> None:
        self._api_key = api_key
        self._http_get = http_get or _http_get

    @property
    def api_key(self) -> str:
        key = self._api_key or os.environ.get("ZEROX_API_KEY")
        if not key:
            raise VenueError("zerox: set ZEROX_API_KEY")
        return key

    def simulate(self, pair: Pair, token_in: Token, token_out: Token, amount_in: int) -> SwapResult:
        query = urllib.parse.urlencode(
            {
                "chainId": pair.chain_id,
                "sellToken": token_in.address,
                "buyToken": token_out.address,
                "sellAmount": str(amount_in),
            }
        )
        data = self._http_get(f"{PRICE_URL}?{query}", {"0x-api-key": self.api_key, "0x-version": "v2"})
        if not data.get("liquidityAvailable", True):
            raise VenueError("zerox: no liquidity available")
        if "buyAmount" not in data:
            raise VenueError(f"zerox: unexpected response {str(data)[:200]}")

        sources = sorted({f.get("source", "?") for f in (data.get("route") or {}).get("fills", [])})
        gas = data.get("gas")
        return SwapResult(
            amount_out=int(data["buyAmount"]),
            gas_estimate=int(gas) if gas is not None else None,
            meta={"sources": sources, "fees": data.get("fees")},
        )
