"""One tracking cycle: fetch all quotes -> compute metrics -> store -> print.

    python -m execution_cost_tracker.run --sizes 1000,10000,100000

Environment:
    BASE_RPC_URL     RPC endpoint for Base (defaults to the public one)
    ZEROX_API_KEY    0x API key; without it the zerox venue records failures
"""

from __future__ import annotations

import argparse
import statistics
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Sequence, TextIO

from . import metrics, registry, storage
from .models import Pair, Quote, ReferenceRate, Side
from .reference import ReferenceSource, StoredMidReference
from .venues import VENUES, Venue

BOTH_SIDES = (Side.SELL, Side.BUY)


def default_pairs() -> list[Pair]:
    """Every pair on every chain listed in config.yaml."""
    return list(registry.get().pairs)


def default_sizes() -> list[float]:
    """The sizes the scheduled run quotes (config.yaml ``cron.sizes``)."""
    return list(registry.get().cron_sizes)


@dataclass
class QuoteRow:
    quote: Quote
    size: float
    ref_mid: float
    deviation_bps: float


@dataclass
class Failure:
    pair: str
    chain: str
    venue: str
    side: str
    size: float
    error: str
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class CycleResult:
    run_id: str
    references: dict[str, ReferenceRate] = field(default_factory=dict)
    rows: list[QuoteRow] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)

    def spreads(self) -> list[dict]:
        """Two-sided view per (pair, chain, venue, size): bid, ask, spread and offsets."""
        grouped: dict[tuple, dict[Side, QuoteRow]] = {}
        for r in self.rows:
            grouped.setdefault((r.quote.pair, r.quote.chain, r.quote.venue, r.size), {})[r.quote.side] = r
        out = []
        for (pair, chain, venue, size), sides in grouped.items():
            sell, buy = sides.get(Side.SELL), sides.get(Side.BUY)
            row = {
                "pair": pair,
                "chain": chain,
                "venue": venue,
                "size": size,
                "bid": sell.quote.price if sell else None,
                "ask": buy.quote.price if buy else None,
                "sell_cost_bps": sell.deviation_bps if sell else None,
                "buy_cost_bps": buy.deviation_bps if buy else None,
                "spread_bps": None,
                "mid_offset_bps": None,
            }
            if sell and buy:
                ref_mid = sell.ref_mid
                row["spread_bps"] = metrics.quoted_spread_bps(sell.quote.price, buy.quote.price, ref_mid)
                row["mid_offset_bps"] = metrics.mid_offset_bps(
                    metrics.midpoint(sell.quote.price, buy.quote.price), ref_mid
                )
            out.append(row)
        return sorted(out, key=lambda d: (d["pair"], d["size"], d["chain"], d["venue"]))


def run_cycle(
    pairs: Sequence[Pair],
    venues: Sequence[Venue],
    reference: ReferenceSource,
    sizes: Sequence[float],
    conn: storage.Connection | None = None,
    run_id: str | None = None,
    sides: Sequence[Side] = BOTH_SIDES,
) -> CycleResult:
    """Quote every (pair, venue, size, side), score against the reference mid,
    and store everything if ``conn`` is given. Venue failures never abort the
    cycle; they are collected in ``result.failures``."""
    sides = [s if isinstance(s, Side) else Side(str(s).lower()) for s in sides]
    result = CycleResult(run_id=run_id or uuid.uuid4().hex[:12])

    for pair in pairs:
        try:
            ref = reference.mid(pair.reference)
        except Exception as e:
            ref = venue_implied_reference(pair, venues, min(sizes))
            if ref is None:
                msg = f"{_err(e)}; and no venue returned a price to estimate one from"
                result.failures.append(Failure(pair.name, pair.chain, "reference", "-", 0.0, msg[:300]))
                continue
        result.references[pair.name] = ref

        for venue in venues:
            if not venue.supports(pair):
                continue
            for size in sizes:
                for side in sides:
                    try:
                        q = venue.quote(pair, side, size, ref.mid)
                    except Exception as e:
                        result.failures.append(Failure(pair.name, pair.chain, venue.name, side.value, size, _err(e)))
                        continue
                    result.rows.append(QuoteRow(q, size, ref.mid, metrics.quote_deviation_bps(q, ref.mid)))

    if conn is not None:
        store(conn, result)
    return result


def venue_implied_reference(pair: Pair, venues: Sequence[Venue], size: float) -> ReferenceRate | None:
    """A stand-in mid when no market price exists: the median price at which
    the venues would buy ``size`` base from you. It's a bid, so it sits about
    half a spread below the true mid; good enough to size buy quotes and
    filter outliers, and only used until a TradingView price arrives."""
    prices = []
    for venue in venues:
        if not venue.supports(pair):
            continue
        try:
            prices.append(venue.quote(pair, Side.SELL, size, 1.0).price)  # mid_hint unused for sells
        except Exception:
            continue
    if not prices:
        return None
    return ReferenceRate(pair=pair.reference, mid=statistics.median(prices), source="venue_implied")


def store(conn: storage.Connection, result: CycleResult) -> None:
    """Write one cycle in a single transaction (all or nothing)."""
    try:
        for ref in result.references.values():
            storage.save_reference(conn, result.run_id, ref)
        for r in result.rows:
            storage.save_quote(conn, result.run_id, r.quote, r.size, r.ref_mid, r.deviation_bps)
        for f in result.failures:
            storage.save_failure(conn, result.run_id, f.ts, f.chain, f.pair, f.venue, f.side, f.size, f.error)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def print_report(result: CycleResult, out: TextIO = sys.stdout) -> None:
    def fmt(v, spec):
        return "-" if v is None else format(v, spec)

    print(f"run {result.run_id}", file=out)
    for name, ref in result.references.items():
        as_of = f" as of {ref.as_of}" if ref.as_of else ""
        print(f"{name}: reference {ref.pair} mid {ref.mid:.5f} ({ref.source}{as_of})", file=out)

    header = f"{'pair':<10} {'size':>9} {'venue':<11} {'bid':>9} {'ask':>9} {'sell bps':>9} {'buy bps':>9} {'spread':>8} {'mid off':>8}"
    rows = result.spreads()
    if rows:
        print(header, file=out)
        print("-" * len(header), file=out)
        for r in rows:
            print(
                f"{r['pair']:<10} {r['size']:>9,.0f} {r['venue']:<11} "
                f"{fmt(r['bid'], '.5f'):>9} {fmt(r['ask'], '.5f'):>9} "
                f"{fmt(r['sell_cost_bps'], '.2f'):>9} {fmt(r['buy_cost_bps'], '.2f'):>9} "
                f"{fmt(r['spread_bps'], '.2f'):>8} {fmt(r['mid_offset_bps'], '.2f'):>8}",
                file=out,
            )
    else:
        print("no quotes", file=out)

    if result.failures:
        print(f"\n{len(result.failures)} failed attempts:", file=out)
        seen: dict[tuple, int] = {}
        for f in result.failures:
            key = (f.pair, f.venue, f.error)
            seen[key] = seen.get(key, 0) + 1
        for (pair, venue, error), n in seen.items():
            print(f"  {pair} {venue} x{n}: {error}", file=out)


def build_venues(names: Iterable[str]) -> list[Venue]:
    venues = []
    for n in names:
        try:
            venues.append(VENUES[n]())
        except KeyError:
            raise SystemExit(f"unknown venue {n!r}; choose from {', '.join(VENUES)}") from None
    return venues


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run one FX tracking cycle.")
    p.add_argument("--db", default="fxtracker.db", help="SQLite path (default: fxtracker.db)")
    p.add_argument("--no-store", action="store_true", help="print only, don't write to the database")
    p.add_argument("--sizes", default=None, help="base-token sizes, comma separated (default: config.yaml cron.sizes)")
    p.add_argument("--venues", default=",".join(VENUES), help="venues, comma separated")
    args = p.parse_args(argv)

    sizes = [float(s) for s in args.sizes.split(",") if s.strip()] if args.sizes else default_sizes()
    venues = build_venues(v.strip() for v in args.venues.split(",") if v.strip())
    conn = None if args.no_store else storage.connect(args.db)
    try:
        result = run_cycle(default_pairs(), venues, StoredMidReference(), sizes, conn)
    finally:
        if conn is not None:
            conn.close()
    print_report(result)
    return 0 if result.rows else 1


def _err(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:300]


if __name__ == "__main__":
    raise SystemExit(main())
