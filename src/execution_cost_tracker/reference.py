"""Traditional FX reference mid rates.

The default source is Frankfurter (European Central Bank reference rates):
free and keyless, but published once per working day around 16:00 CET, so
intraday comparisons carry that staleness. Swap in a live source by
implementing :class:`ReferenceSource`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Protocol

from .models import ReferenceRate

FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest"

# Python's default "Python-urllib/x.y" user agent is blocked by many sites,
# so identify the app explicitly on every outgoing request.
USER_AGENT = "execution-cost-tracker/0.3 (+https://github.com/rd1409/execution-cost-tracker)"


class ReferenceSource(Protocol):
    def mid(self, pair: str) -> ReferenceRate:
        """Mid rate for a pair written as "BASE/QUOTE", e.g. "EUR/USD"."""


def split_pair(pair: str) -> tuple[str, str]:
    base, sep, quote = pair.upper().partition("/")
    if not sep or not base or not quote:
        raise ValueError(f"pair must look like 'EUR/USD', got {pair!r}")
    return base, quote


class FrankfurterReference:
    """ECB reference rates via api.frankfurter.dev."""

    def __init__(self, http_get: Callable[[str], dict] | None = None) -> None:
        self._http_get = http_get or _http_get

    def mid(self, pair: str) -> ReferenceRate:
        base, quote = split_pair(pair)
        url = f"{FRANKFURTER_URL}?{urllib.parse.urlencode({'base': base, 'symbols': quote})}"
        data = self._http_get(url)
        try:
            rate = float(data["rates"][quote])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"Frankfurter: no {pair} rate in {str(data)[:200]}") from e
        return ReferenceRate(pair=f"{base}/{quote}", mid=rate, source="frankfurter-ecb", as_of=data.get("date"))


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
        raise KeyError(f"no static rate for {key}")


def _http_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        raise RuntimeError(f"reference HTTP {e.code} from {url}: {body}") from e
