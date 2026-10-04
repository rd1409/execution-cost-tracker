"""Request handling for the web API, kept free of web-framework imports so it
can be tested offline. ``api.py`` is a thin FastAPI layer over these functions.

Everything a user can choose (pairs, chains, tokens, limits) comes from the
registry in config.yaml; see ``registry.py``.
"""

from __future__ import annotations

import hmac
import json
import math
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Sequence

from . import market_hours, metrics, registry, storage
from .guardrails import RateLimited, SlidingWindowLimiter, TTLCache
from .models import Pair, Side
from .reference import FrankfurterReference, ReferenceSource
from .registry import Registry
from .run import CycleResult, build_venues, default_pairs, default_sizes, run_cycle
from .venues import VENUES, Venue

VenueFactory = Callable[[Iterable[str]], list[Venue]]

VENUE_LABELS = {
    "uniswap_v3": "Uniswap v3",
    "uniswap_v4": "Uniswap v4",
    "aerodrome": "Aerodrome",
    "zerox": "0x (aggregator)",
}
SIDE_CHOICES = ("both", "buy", "sell")

__all__ = [
    "QuoteParams",
    "RateLimited",
    "WebhookError",
    "config_view",
    "cron_cycle",
    "guarded_quote",
    "handle_tradingview_webhook",
    "market_mid_status",
    "quote",
]

MidLookup = Callable[[str], "dict | None"]


def max_notional(reg: Registry | None = None) -> float:
    """Largest notional the public endpoint accepts: MAX_NOTIONAL env var,
    else config.yaml ``limits.max_notional``."""
    env = os.environ.get("MAX_NOTIONAL")
    if env:
        return float(env)
    return (reg or registry.get()).max_notional


def venues_for(pair: Pair) -> list[str]:
    """Venue names that can quote ``pair`` (i.e. have contracts on its chain)."""
    return [name for name, cls in VENUES.items() if cls().supports(pair)]


@dataclass
class QuoteParams:
    """Validated inputs for one on-demand quote request.

    Raises ValueError with a user-readable message on any invalid input.
    """

    pair: str = "EURC/USDC"
    notional: float = 10_000.0
    side: str = "both"
    source_chain: str = "base"
    destination_chain: str | None = None
    venues: list[str] | None = None
    registry: Registry | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        reg = self.registry or registry.get()
        self.registry = reg

        self.side = str(self.side).lower()
        if self.side not in SIDE_CHOICES:
            raise ValueError(f"side must be one of {', '.join(SIDE_CHOICES)}")

        if self.source_chain not in reg.chains:
            raise ValueError(f"unknown source chain {self.source_chain!r}; choose from {', '.join(reg.chains)}")
        if self.destination_chain in (None, ""):
            self.destination_chain = self.source_chain
        if self.destination_chain not in reg.chains:
            raise ValueError(
                f"unknown destination chain {self.destination_chain!r}; choose from {', '.join(reg.chains)}"
            )
        if self.destination_chain != self.source_chain:
            raise ValueError(
                "cross-chain routes aren't supported yet; choose the same source and destination chain"
            )

        pair = reg.pair(self.pair, self.source_chain)
        if pair is None:
            on_chain = sorted(p.name for p in reg.pairs_on(self.source_chain))
            raise ValueError(f"pair {self.pair!r} isn't listed on {self.source_chain}; choose from {', '.join(on_chain)}")
        self._pair = pair

        try:
            self.notional = float(self.notional)
        except (TypeError, ValueError):
            raise ValueError("notional must be a number") from None
        lo, hi = reg.min_notional, max_notional(reg)
        if not math.isfinite(self.notional) or not lo <= self.notional <= hi:
            raise ValueError(f"notional must be between {lo:,.0f} and {hi:,.0f} {pair.base.symbol}")

        available = venues_for(pair)
        if self.venues is None:
            self.venues = available
        self.venues = list(dict.fromkeys(self.venues))  # drop duplicates, keep order
        if not self.venues:
            raise ValueError("choose at least one venue")
        unknown = [v for v in self.venues if v not in VENUES]
        if unknown:
            raise ValueError(f"unknown venue(s) {', '.join(unknown)}; choose from {', '.join(VENUES)}")
        unsupported = [v for v in self.venues if v not in available]
        if unsupported:
            raise ValueError(f"{', '.join(unsupported)} can't quote on {self.source_chain}")

    @property
    def pair_obj(self) -> Pair:
        return self._pair

    @property
    def sides(self) -> tuple[Side, ...]:
        return (Side.SELL, Side.BUY) if self.side == "both" else (Side(self.side),)

    def cache_key(self) -> tuple:
        return (
            self.pair,
            self.source_chain,
            self.destination_chain,
            self.side,
            round(self.notional, 6),
            tuple(sorted(self.venues)),
        )

    def as_dict(self) -> dict:
        return {
            "pair": self.pair,
            "notional": self.notional,
            "side": self.side,
            "source_chain": self.source_chain,
            "destination_chain": self.destination_chain,
            "venues": list(self.venues),
        }


# --- config for the front end ----------------------------------------------


def config_view(reg: Registry | None = None) -> dict:
    """Everything the front end needs to build its form: chains, tokens,
    pairs, which venues work where, and the limits."""
    reg = reg or registry.get()
    pairs: dict[str, dict] = {}
    for p in reg.pairs:
        entry = pairs.setdefault(
            p.name,
            {
                "pair": p.name,
                "base": p.base.symbol,
                "quote": p.quote.symbol,
                "reference": p.reference,
                "chains": [],
                "venues": {},
            },
        )
        entry["chains"].append(p.chain)
        entry["venues"][p.chain] = venues_for(p)
    return {
        "chains": [
            {
                "key": c.key,
                "name": c.name,
                "chain_id": c.chain_id,
                "tokens": [asdict(t) for t in c.tokens.values()],
            }
            for c in reg.chains.values()
        ],
        "pairs": list(pairs.values()),
        "venues": [{"name": n, "label": VENUE_LABELS.get(n, n)} for n in VENUES],
        "sides": list(SIDE_CHOICES),
        "limits": {"min_notional": reg.min_notional, "max_notional": max_notional(reg)},
        "quality": {"max_distance_from_mid_pct": reg.max_distance_from_mid_pct},
        "market_hours": market_hours.status(datetime.now(timezone.utc)),
        "market_mid": {"max_age_seconds": reg.market_mid_max_age_seconds, "source": "tradingview"},
        "guardrails": {
            "per_client_per_minute": reg.per_client_per_minute,
            "cache_seconds": reg.cache_seconds,
        },
        "cross_chain": False,
    }


def list_pairs(reg: Registry | None = None) -> list[dict]:
    """One entry per (pair, chain), with token details and usable venues."""
    reg = reg or registry.get()
    return [
        {
            "pair": p.name,
            "chain": p.chain,
            "chain_id": p.chain_id,
            "reference": p.reference,
            "base": asdict(p.base),
            "quote": asdict(p.quote),
            "venues": venues_for(p),
        }
        for p in reg.pairs
    ]


# --- quoting -----------------------------------------------------------------


def quote(
    params: QuoteParams,
    reference: ReferenceSource | None = None,
    venue_factory: VenueFactory = build_venues,
    mid_lookup: MidLookup | None = None,
    now: datetime | None = None,
) -> dict:
    """Quote one pair at one notional across the chosen venues. Not stored.

    Quotes priced more than ``quality.max_distance_from_mid_pct`` from the
    reference rate are moved to ``excluded`` and left out of ``quotes``,
    ``rows`` and ``best``. Nothing is remembered between requests, so a venue
    that comes back inside the band is included again next time.
    """
    result = run_cycle(
        [params.pair_obj],
        venue_factory(params.venues),
        reference or FrankfurterReference(),
        [params.notional],
        sides=params.sides,
    )
    band = params.registry.max_distance_from_mid_pct
    excluded = drop_outside_band(result, band)
    out = result_to_dict(result)
    out["request"] = params.as_dict()
    out["best"] = best_by_side(result)
    out["band_pct"] = band
    out["excluded"] = [
        {**_quote_dict(r), "offset_pct": metrics.offset_pct(r.quote.price, r.ref_mid)} for r in excluded
    ]

    # Compare with the live market mid during FX hours; None (N/A) otherwise.
    mm = market_mid_status(params.pair_obj.reference, now or datetime.now(timezone.utc), mid_lookup, params.registry)
    out["market_mid"] = mm
    for q in out["quotes"] + list(out["best"].values()):
        q["vs_market_mid_bps"] = (
            metrics.deviation_bps(q["price"], mm["mid"], q["side"]) if mm["available"] else None
        )
    return out


# --- live market mid -------------------------------------------------------------


def stored_mid_lookup(pair: str) -> dict | None:
    """Latest market mid for ``pair`` from the database."""
    conn = storage.connect()
    try:
        return storage.latest_market_mid(conn, pair)
    finally:
        conn.close()


def market_mid_status(
    reference_pair: str,
    now: datetime,
    lookup: MidLookup | None = None,
    reg: Registry | None = None,
) -> dict:
    """Whether a market mid can be used right now, and why not if it can't.

    Available only when the FX market is open (Monday 05:00 Sydney to Friday
    17:00 New York) and the last TradingView price is fresher than
    ``market_mid.max_age_seconds``. ``reason`` explains any N/A.
    """
    reg = reg or registry.get()
    hours = market_hours.status(now)
    out = {
        "pair": reference_pair,
        "available": False,
        "mid": None,
        "as_of": None,
        "age_seconds": None,
        "source": None,
        "reason": None,
        "market_open": hours["open"],
        "next_change": hours["next_change"],
        "hours": hours["hours"],
    }
    if not hours["open"]:
        out["reason"] = "FX market closed"
        return out
    try:
        row = (lookup or stored_mid_lookup)(reference_pair)
    except Exception as e:  # database down or not configured
        out["reason"] = f"market mid unavailable ({type(e).__name__})"
        return out
    if not row:
        out["reason"] = "no market mid received from TradingView yet"
        return out
    age = (now - row["received_at"]).total_seconds()
    out.update(mid=row["mid"], as_of=row["received_at"].isoformat(), age_seconds=round(age, 1), source=row["source"])
    if age > reg.market_mid_max_age_seconds:
        mins = int(age // 60)
        out["reason"] = f"last TradingView price is {mins} min old" if mins else f"last TradingView price is {int(age)}s old"
        return out
    out["available"] = True
    return out


class WebhookError(Exception):
    """A rejected webhook call; ``status`` is the HTTP status to return."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def handle_tradingview_webhook(
    body: bytes | str,
    client_ip: str | None,
    secret: str | None,
    connect: Callable[[], object] = storage.connect,
    now: datetime | None = None,
    reg: Registry | None = None,
) -> dict:
    """Store a market mid sent by a TradingView alert.

    Expected JSON body (set as the alert message in TradingView):
        {"secret": "...", "ticker": "{{ticker}}", "price": {{close}}, "time": "{{timenow}}"}
    "bid" and "ask" may be sent instead of "price"; their midpoint is stored.
    TradingView can't send custom headers, so the shared secret travels in
    the body; requests are also limited to TradingView's published addresses.
    """
    reg = reg or registry.get()
    if not secret:
        raise WebhookError(503, "TRADINGVIEW_WEBHOOK_SECRET isn't set on the server")
    if reg.enforce_ip_allowlist and client_ip not in reg.tradingview_ips:
        raise WebhookError(403, f"{client_ip} isn't one of TradingView's webhook addresses")
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        raise WebhookError(400, "body must be JSON; see the README for the alert message format") from None
    if not isinstance(data, dict):
        raise WebhookError(400, "body must be a JSON object")
    if not hmac.compare_digest(str(data.get("secret", "")), secret):
        raise WebhookError(401, "wrong or missing secret")

    ticker = str(data.get("ticker", ""))
    pair = reg.reference_for_symbol(ticker)
    if pair is None:
        raise WebhookError(400, f"ticker {ticker!r} isn't listed under market_mid.symbols in config.yaml")

    def number(key: str) -> float | None:
        v = data.get(key)
        if v is None:
            return None
        try:
            x = float(v)
        except (TypeError, ValueError):
            raise WebhookError(400, f"{key} must be a number, got {v!r}") from None
        if not math.isfinite(x) or x <= 0:
            raise WebhookError(400, f"{key} must be a positive number")
        return x

    bid, ask = number("bid"), number("ask")
    if bid is not None and ask is not None:
        if ask < bid:
            raise WebhookError(400, "ask must not be below bid")
        mid = (bid + ask) / 2
    else:
        mid = number("price")
        if mid is None:
            mid = number("close")
        if mid is None:
            raise WebhookError(400, "send price (or bid and ask)")

    now = now or datetime.now(timezone.utc)
    conn = connect()
    try:
        storage.save_market_mid(conn, pair, mid, "tradingview", now, ticker=ticker, bar_time=data.get("time"))
    finally:
        conn.close()
    return {"ok": True, "pair": pair, "mid": mid, "received_at": now.isoformat()}


def drop_outside_band(result: CycleResult, max_pct: float) -> list:
    """Remove rows priced more than ``max_pct`` percent from their reference
    mid from ``result.rows`` (in place) and return the removed rows."""
    kept, dropped = [], []
    for r in result.rows:
        (kept if metrics.within_band(r.quote.price, r.ref_mid, max_pct) else dropped).append(r)
    result.rows = kept
    return dropped


def best_by_side(result: CycleResult) -> dict:
    """The best venue for each side that got quotes: highest price to sell,
    lowest to buy."""
    best = {}
    for side in (Side.SELL, Side.BUY):
        rows = [r for r in result.rows if r.quote.side is side]
        top = metrics.best_quote(r.quote for r in rows)
        if top is None:
            continue
        row = next(r for r in rows if r.quote is top)
        best[side.value] = {
            "side": side.value,
            "venue": top.venue,
            "price": top.price,
            "deviation_bps": row.deviation_bps,
            "base_amount": top.base_amount,
            "quote_amount": top.quote_amount,
        }
    return best


@dataclass
class Guard:
    """Per-process guardrail state for the public quote endpoint."""

    per_client: SlidingWindowLimiter
    global_: SlidingWindowLimiter
    cache: TTLCache
    lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_registry(cls, reg: Registry, clock: Callable[[], float] | None = None) -> "Guard":
        kw = {"clock": clock} if clock else {}
        return cls(
            per_client=SlidingWindowLimiter(reg.per_client_per_minute, 60.0, **kw),
            global_=SlidingWindowLimiter(reg.global_per_minute, 60.0, **kw),
            cache=TTLCache(reg.cache_seconds, **kw),
        )


_default_guard: Guard | None = None


def default_guard() -> Guard:
    global _default_guard
    if _default_guard is None:
        _default_guard = Guard.from_registry(registry.get())
    return _default_guard


def guarded_quote(params: QuoteParams, client: str, guard: Guard | None = None, **quote_kwargs) -> dict:
    """:func:`quote` behind the public-endpoint guardrails.

    Identical requests within ``cache_seconds`` get the cached answer (and
    don't count against the rate limits). Otherwise the caller's per-client
    limit and the global limit both apply; exceeding either raises
    :class:`RateLimited`.
    """
    g = guard or default_guard()
    key = params.cache_key()
    with g.lock:
        hit = g.cache.get(key)
        if hit is not None:
            return {**hit, "cached": True, "cache_age_seconds": round(g.cache.age(key) or 0.0, 1)}
        wait = g.per_client.check(client)
        if wait:
            raise RateLimited("client", wait)
        wait = g.global_.check("*")
        if wait:
            raise RateLimited("global", wait)
        g.per_client.record(client)
        g.global_.record("*")

    out = quote(params, **quote_kwargs)  # network calls happen outside the lock

    if out["quotes"]:  # don't cache total failures; let the next try go through
        with g.lock:
            g.cache.set(key, out)
    return {**out, "cached": False, "cache_age_seconds": 0.0}


# --- scheduled run -------------------------------------------------------------


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
    sizes: Sequence[float] | None = None,
    pairs: Sequence[Pair] | None = None,
) -> dict:
    """The scheduled job: quote every pair, venue and size, then store it all."""
    conn = connect()
    try:
        result = run_cycle(
            pairs if pairs is not None else default_pairs(),
            venues if venues is not None else build_venues(VENUES),
            reference or FrankfurterReference(),
            sizes if sizes is not None else default_sizes(),
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
        "quotes": [_quote_dict(r) for r in result.rows],
        "failures": [
            {"venue": f.venue, "pair": f.pair, "side": f.side, "size": f.size, "error": f.error, "timestamp": _iso(f.ts)}
            for f in result.failures
        ],
        "errors": summarize_failures(result),
    }


def _quote_dict(r) -> dict:
    return {
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


def _iso(ts: datetime) -> str:
    return ts.isoformat()
