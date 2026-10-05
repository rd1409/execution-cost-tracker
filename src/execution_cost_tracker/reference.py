"""Reference mid rates used to size buy quotes, filter outliers and centre the chart.

The source is the latest market mid stored from TradingView (see the
``/api/tradingview/webhook`` endpoint), whatever its age: over a weekend that's
Friday's last price, which is still a good anchor. The page's "vs mkt mid"
column applies stricter rules (FX hours and freshness) on top of this; see
``service.market_mid_status``.

If no TradingView price has ever been stored, ``run.run_cycle`` falls back to a
mid implied by the venues' own prices.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Protocol

from .models import ReferenceRate

# Python's default "Python-urllib/x.y" user agent is blocked by many sites,
# so identify the app explicitly on every outgoing request.
USER_AGENT = "execution-cost-tracker/0.3 (+https://github.com/rd1409/execution-cost-tracker)"

MidLookup = Callable[[str], "dict | None"]


class ReferenceSource(Protocol):
    def mid(self, pair: str) -> ReferenceRate:
        """Mid rate for a pair written as "BASE/QUOTE", e.g. "EUR/USD"."""


class NoReferenceError(LookupError):
    """No reference price is available for a pair."""


def split_pair(pair: str) -> tuple[str, str]:
    base, sep, quote = pair.upper().partition("/")
    if not sep or not base or not quote:
        raise ValueError(f"pair must look like 'EUR/USD', got {pair!r}")
    return base, quote


def _stored_lookup(pair: str) -> dict | None:
    from . import storage  # imported here so tests and the CLI don't need a database

    conn = storage.connect()
    try:
        return storage.latest_market_mid(conn, pair)
    finally:
        conn.close()


class StoredMidReference:
    """The most recent TradingView market mid in the database, of any age."""

    def __init__(self, lookup: MidLookup | None = None) -> None:
        self._lookup = lookup or _stored_lookup

    def mid(self, pair: str) -> ReferenceRate:
        base, quote = split_pair(pair)
        key = f"{base}/{quote}"
        row = self._lookup(key)
        if not row:
            raise NoReferenceError(f"no TradingView price for {key} has been received yet")
        received = row["received_at"]
        if isinstance(received, str):
            received = datetime.fromisoformat(received)
        return ReferenceRate(
            pair=key,
            mid=float(row["mid"]),
            source=str(row.get("source") or "tradingview"),
            timestamp=received,
            as_of=received.isoformat(),
        )


class StaticReference:
    """Fixed mids, for tests, backfills, or a manually entered rate."""

    def __init__(self, mids: dict[str, float], source: str = "static") -> None:
        self._mids = {k.upper(): v for k, v in mids.items()}
        self._source = source

    def mid(self, pair: str) -> ReferenceRate:
        base, quote = split_pair(pair)
        key = f"{base}/{quote}"
        if key in self._mids:
            return ReferenceRate(pair=key, mid=self._mids[key], source=self._source)
        inverse = f"{quote}/{base}"
        if inverse in self._mids:
            return ReferenceRate(pair=key, mid=1 / self._mids[inverse], source=self._source)
        raise NoReferenceError(f"no static rate for {key}")
