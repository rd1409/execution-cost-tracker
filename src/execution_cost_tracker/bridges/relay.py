"""Relay: solver-filled bridge. Quotes via POST /quote/v2.

``details.currencyOut`` is what arrives after Relay's fees, and
``fees.gas.amountUsd`` is Relay's estimate of the source-chain transaction
fee the user pays.

Docs: https://docs.relay.link/references/api/get-quote-v2
Relay requires an API key on /quote/v2 from 2026-10-02: set RELAY_API_KEY
(from https://dashboard.relay.link). It's sent as the x-api-key header.
"""

from __future__ import annotations

import os

from .. import httpjson
from ..registry import Registry
from .base import Bridge, BridgeError, BridgeQuote, endpoints, quote_wallet

QUOTE_URL = "https://api.relay.link/quote/v2"


class Relay(Bridge):
    name = "relay"
    label = "Relay"

    def __init__(self, http_post: httpjson.HttpPost | None = None) -> None:
        self._post = http_post or (lambda url, body, headers: httpjson.post_json(url, body, headers, timeout=12))

    def quote(self, reg: Registry, token: str, from_chain: str, to_chain: str, amount: float) -> BridgeQuote:
        a, ta, b, tb = endpoints(reg, token, from_chain, to_chain)
        body = {
            "user": quote_wallet(reg, a),
            "recipient": quote_wallet(reg, b),
            "originChainId": a.bridge_id(self.name),
            "destinationChainId": b.bridge_id(self.name),
            "originCurrency": ta.address,
            "destinationCurrency": tb.address,
            "amount": str(ta.to_raw(amount)),
            "tradeType": "EXACT_INPUT",
        }
        key = os.environ.get("RELAY_API_KEY", "").strip()  # pasted keys often carry a trailing newline
        headers = {"x-api-key": key} if key else {}
        try:
            data = self._post(QUOTE_URL, body, headers)
        except httpjson.HttpError as e:
            hint = " (set RELAY_API_KEY)" if e.status in (401, 403) and not headers else ""
            raise BridgeError(f"Relay: {e}{hint}"[:300]) from e
        details = (data or {}).get("details") or {}
        cur_out = details.get("currencyOut") or {}
        try:
            decimals = int((cur_out.get("currency") or {}).get("decimals", tb.decimals))
            out = int(cur_out["amount"]) / 10**decimals
        except (KeyError, TypeError, ValueError):
            raise BridgeError(f"Relay: unexpected response {str(data)[:200]}") from None
        if out <= 0:
            raise BridgeError("Relay: nothing would arrive")
        fees = (data or {}).get("fees") or {}
        gas_usd = _usd(fees.get("gas"))
        return BridgeQuote(
            provider=self.name,
            token=token,
            from_chain=from_chain,
            to_chain=to_chain,
            amount_in=amount,
            amount_out=out,
            origin_gas_usd=gas_usd,
            gas_units=None if gas_usd is not None else reg.routing.gas_units["approve"] + reg.routing.gas_units["bridge"],
            eta_seconds=_float(details.get("timeEstimate")),
            route="Relay solver fill",
            meta={"relayer_fee_usd": _usd(fees.get("relayer"))},
        )


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _usd(fee) -> float | None:
    return _float((fee or {}).get("amountUsd")) if isinstance(fee, dict) else None
