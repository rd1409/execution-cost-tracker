"""Request handling for the web API, kept free of web-framework imports so it
can be tested offline. ``api.py`` is a thin FastAPI layer over these functions.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Sequence

from . import storage
from .reference import FrankfurterReference, ReferenceSource
from .run import DEFAULT_PAIRS, DEFAULT_SIZES, CycleResult, build_venues, run_cycle
from .venues import VENUES, Venue

DEFAULT_MAX_NOTIONAL = 1_000_000.0

VenueFactory = Callable[[Iterable[str]], list[Venue]]


def max_notional() -> float:
    """Largest notional the public endpoint accepts (MAX_NOTIONAL env var)."""
    return float(os.environ.get("MAX_NOTIONAL", DEFAULT_MAX_NOTIONAL))


@dataclass
class QuoteParams:
    """Validated inputs for one on-demand quote request."""

    pair: str = "EURC/USDC"
    notional: float = 10_000.0
    venues: list[str] = field(default_factory=lambda: list(VENUES))

    def __post_init__(self) -> None:
        names = {p.name for p in DEFAULT_PAIRS}
        if self.pair not in names:
            raise ValueError(f"unknown pair {self.pair!r}; choose from {', '.join(sorted(names))}")
        limit = max_notional()
        if not 0 < self.notional <= limit:
            raise ValueError(f"notional must be greater than 0 and at most {limit:,.0f}")
        if not self.venues:
            raise ValueError("choose at least one venue")
        unknown = [v for v in self.venues if v not in VENUES]
        if unknown:
            raise ValueError(f"unknown venue(s) {', '.join(unknown)}; choose from {', '.join(VENUES)}")


def list_pairs() -> list[dict]:
    """The pairs and venues a front end can offer."""
    return [
        {
            "pair": p.name,
            "chain": p.chain,
            "chain_id": p.chain_id,
            "reference": p.reference,
            "base": asdict(p.base),
            "quote": asdict(p.quote),
            "venues": [name for name, cls in VENUES.items() if cls().supports(p)],
        }
        for p in DEFAULT_PAIRS
    ]


def quote(
    params: QuoteParams,
    reference: ReferenceSource | None = None,
    venue_factory: VenueFactory = build_venues,
) -> dict:
    """Quote one pair at one notional across the chosen venues. Not stored."""
    pairs = [p for p in DEFAULT_PAIRS if p.name == params.pair]
    result = run_cycle(pairs, venue_factory(params.venues), reference or FrankfurterReference(), [params.notional])
    return result_to_dict(result)


def check_cron_auth(authorization: str | None, secret: str | None) -> bool:
    """True if the Authorization header carries ``Bearer <CRON_SECRET>``.

    Always False when no secret is configured, so the endpoint is closed by
    default rather than open to anyone.
    """
    if not secret or not authorization:
        return False
    return hmac.compare_digest(authorization, f"Bearer {secret}")


def cron_cycle(
    connect: Callable[[], object] = storage.connect,
    reference: ReferenceSource | None = None,
    venues: Sequence[Venue] | None = None,
    sizes: Sequence[float] = DEFAULT_SIZES,
) -> dict:
    """The scheduled job: quote every pair, venue and size, then store it all."""
    conn = connect()
    try:
        result = run_cycle(
            DEFAULT_PAIRS,
            venues if venues is not None else build_venues(VENUES),
            reference or FrankfurterReference(),
            sizes,
            conn,
        )
    finally:
        conn.close()
    return {
        "run_id": result.run_id,
        "quotes": len(result.rows),
        "failures": len(result.failures),
        "references": {k: v.mid for k, v in result.references.items()},
        "errors": summarize_failures(result),
    }


def summarize_failures(result: CycleResult, limit: int = 10) -> list[str]:
    """Distinct failure messages as "pair venue: error (xN)", most frequent first.

    Lets a caller (or a person with curl) see why a run failed without
    querying the failures table.
    """
    counts: dict[str, int] = {}
    for f in result.failures:
        key = f"{f.pair} {f.venue}: {f.error}"
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    return [k if n == 1 else f"{k} (x{n})" for k, n in ranked]


def result_to_dict(result: CycleResult) -> dict:
    """A JSON-serialisable view of a cycle for API responses."""
    return {
        "run_id": result.run_id,
        "references": {
            name: {"pair": r.pair, "mid": r.mid, "source": r.source, "as_of": r.as_of, "timestamp": _iso(r.timestamp)}
            for name, r in result.references.items()
        },
        "rows": result.spreads(),
        "quotes": [
            {
                "venue": r.quote.venue,
                "pair": r.quote.pair,
                "chain": r.quote.chain,
                "side": r.quote.side.value,
                "size": r.size,
                "base_amount": r.quote.base_amount,
                "quote_amount": r.quote.quote_amount,
                "price": r.quote.price,
                "deviation_bps": r.deviation_bps,
                "gas_estimate": r.quote.gas_estimate,
                "meta": r.quote.meta,
                "timestamp": _iso(r.quote.timestamp),
            }
            for r in result.rows
        ],
        "failures": [
            {"venue": f.venue, "pair": f.pair, "side": f.side, "size": f.size, "error": f.error, "timestamp": _iso(f.ts)}
            for f in result.failures
        ],
    }


def _iso(ts: datetime) -> str:
    return ts.isoformat()
