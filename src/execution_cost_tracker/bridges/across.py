"""Across: relayer-filled bridge. Quotes via GET /api/suggested-fees.

The response gives the amount that arrives (``outputAmount``) after
Across's relayer and LP fees, an estimated fill time, and deposit limits.
It doesn't give the depositor's own gas, so that's estimated from
``routing.gas_units`` (approve + bridge deposit).

Docs: https://docs.across.to/api-reference/suggested-fees/get
ACROSS_API_KEY is optional and sent as a Bearer token when set.
"""

from __future__ import annotations

import os
import urllib.parse

from .. import httpjson
from ..registry import Registry
from .base import Bridge, BridgeError, BridgeQuote, endpoints, quote_wallet

SUGGESTED_FEES_URL = "https://app.across.to/api/suggested-fees"


class Across(Bridge):
    name = "across"
    label = "Across"

    def __init__(self, http_get: httpjson.HttpGet | None = None) -> None:
        self._get = http_get or (lambda url, headers: httpjson.get_json(url, headers, timeout=12))

    def quote(self, reg: Registry, token: str, from_chain: str, to_chain: str, amount: float) -> BridgeQuote:
        a, ta, b, tb = endpoints(reg, token, from_chain, to_chain)
        params = {
            "inputToken": ta.address,
            "outputToken": tb.address,
            "originChainId": a.bridge_id(self.name),
            "destinationChainId": b.bridge_id(self.name),
            "amount": str(ta.to_raw(amount)),
            "recipient": quote_wallet(reg, b),
        }
        headers = {}
        if os.environ.get("ACROSS_API_KEY"):
            headers["Authorization"] = f"Bearer {os.environ['ACROSS_API_KEY']}"
        try:
            data = self._get(f"{SUGGESTED_FEES_URL}?{urllib.parse.urlencode(params)}", headers)
        except httpjson.HttpError as e:
            raise BridgeError(f"Across: {e}"[:300]) from e
        if not isinstance(data, dict) or "outputAmount" not in data:
            raise BridgeError(f"Across: unexpected response {str(data)[:200]}")
        if data.get("isAmountTooLow"):
            raise BridgeError("Across: amount is below the minimum for this route")
        max_deposit = (data.get("limits") or {}).get("maxDeposit")
        if max_deposit is not None and int(max_deposit) < ta.to_raw(amount):
            raise BridgeError(f"Across: amount is above this route's limit of {ta.from_raw(int(max_deposit)):,.0f} {token}")
        out = tb.from_raw(int(data["outputAmount"]))
        if out <= 0:
            raise BridgeError("Across: nothing would arrive")
        units = reg.routing.gas_units
        fee = data.get("totalRelayFee") or {}
        return BridgeQuote(
            provider=self.name,
            token=token,
            from_chain=from_chain,
            to_chain=to_chain,
            amount_in=amount,
            amount_out=out,
            gas_units=units["approve"] + units["bridge"],
            eta_seconds=_num(data.get("estimatedFillTimeSec")),
            route="Across relayer fill",
            meta={"relay_fee": ta.from_raw(int(fee["total"])) if fee.get("total") else None},
        )


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
