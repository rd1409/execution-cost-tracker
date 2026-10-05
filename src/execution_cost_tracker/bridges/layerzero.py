"""LayerZero: Stargate stablecoin pools (Tempo's native USDC.e bridge).

Quotes via the LayerZero Value Transfer API, POST /v1/quotes, which needs an
API key in LAYERZERO_API_KEY (sent as x-api-key). The best of the returned
quotes (most arriving) is used.

``dstAmount`` is what arrives. LayerZero's messaging fee is paid on top in the
source chain's gas token (on Tempo, in a USD stablecoin), so it's taken from
``feeUsd`` minus whatever is already deducted from the token amount.

Docs: https://docs.layerzero.network/v2/developers/value-transfer-api/api-reference/quotes
The API doesn't support EURC.e on Tempo yet, so EURC isn't asked about by default.
"""

from __future__ import annotations

import os

from .. import httpjson
from ..registry import Registry
from .base import Bridge, BridgeError, BridgeQuote, endpoints, quote_wallet

QUOTES_URL = "https://transfer.layerzero-api.com/v1/quotes"


class LayerZero(Bridge):
    name = "layerzero"
    label = "LayerZero (Stargate)"

    def __init__(self, http_post: httpjson.HttpPost | None = None) -> None:
        self._post = http_post or (lambda url, body, headers: httpjson.post_json(url, body, headers, timeout=12))

    def quote(self, reg: Registry, token: str, from_chain: str, to_chain: str, amount: float) -> BridgeQuote:
        key = os.environ.get("LAYERZERO_API_KEY")
        if not key:
            raise BridgeError("LayerZero: set LAYERZERO_API_KEY to get quotes")
        a, ta, b, tb = endpoints(reg, token, from_chain, to_chain)
        body = {
            "srcChainKey": a.bridge_id(self.name),
            "dstChainKey": b.bridge_id(self.name),
            "srcTokenAddress": ta.address,
            "dstTokenAddress": tb.address,
            "srcWalletAddress": quote_wallet(reg, a),
            "dstWalletAddress": quote_wallet(reg, b),
            "amount": str(ta.to_raw(amount)),
            "options": {"amountType": "EXACT_SRC_AMOUNT"},
        }
        try:
            data = self._post(QUOTES_URL, body, {"x-api-key": key})
        except httpjson.HttpError as e:
            raise BridgeError(f"LayerZero: {e}"[:300]) from e
        quotes = [q for q in (data or {}).get("quotes") or [] if isinstance(q, dict) and q.get("dstAmount")]
        if not quotes:
            reason = (data or {}).get("error") or (data or {}).get("rejectedQuotes") or "no route"
            raise BridgeError(f"LayerZero: no quote ({str(reason)[:200]})")
        best = max(quotes, key=lambda q: int(q["dstAmount"]))
        out = tb.from_raw(int(best["dstAmount"]))
        if out <= 0:
            raise BridgeError("LayerZero: nothing would arrive")

        fee_usd = _float(best.get("feeUsd")) or 0.0
        src_usd, dst_usd = _float(best.get("srcAmountUsd")), _float(best.get("dstAmountUsd"))
        if src_usd is not None and dst_usd is not None:
            fee_usd = max(0.0, fee_usd - max(0.0, src_usd - dst_usd))  # don't count the token deduction twice
        steps = [s.get("type") for s in best.get("routeSteps") or [] if isinstance(s, dict) and s.get("type")]
        units = reg.routing.gas_units
        return BridgeQuote(
            provider=self.name,
            token=token,
            from_chain=from_chain,
            to_chain=to_chain,
            amount_in=amount,
            amount_out=out,
            fee_usd=fee_usd,
            gas_units=units["approve"] + units["bridge"],
            eta_seconds=_float((best.get("duration") or {}).get("estimated")),
            route=", ".join(steps) or "Stargate",
            meta={"quote_id": best.get("id"), "fee_usd_reported": _float(best.get("feeUsd"))},
        )


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
