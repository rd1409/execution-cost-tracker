"""Coinbase order-book venue, offline: fake books, fake SDK, fake HTTP."""

import io
import json
import sys
import types

import pytest

from execution_cost_tracker import Pair, Side, Token, registry
from execution_cost_tracker.venues import VenueError, coinbase

USDC = Token("USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6)
EURC = Token("EURC", "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42", 6)


def pair(**cb):
    params = {"product_id": "EURC-USDC", **cb}
    return Pair(base=EURC, quote=USDC, reference="EUR/USD", venue_params={"coinbase": params})


BOOK = {
    "pricebook": {
        "product_id": "EURC-USDC",
        "bids": [{"price": "1.1230", "size": "300000"}, {"price": "1.1228", "size": "500000"}, {"price": "1.1225", "size": "400000"}],
        "asks": [{"price": "1.1234", "size": "250000"}, {"price": "1.1238", "size": "200000"}, {"price": "1.1241", "size": "1000000"}],
        "time": "2026-10-05T02:40:00Z",
    },
    "mid_market": "1.1232",
}


class Fetcher:
    def __init__(self, book=BOOK):
        self.book, self.calls = book, []

    def __call__(self, product_id, limit):
        self.calls.append((product_id, limit))
        return self.book


def test_sell_one_million_eurc_uses_weighted_average():
    f = Fetcher()
    q = coinbase.CoinbaseOrderBook(fetcher=f).quote(pair(), Side.SELL, 1_000_000, mid_hint=1.1232)
    expected = (300_000 * 1.1230 + 500_000 * 1.1228 + 200_000 * 1.1225) / 1_000_000
    assert q.venue == "coinbase" and q.base_amount == 1_000_000
    assert q.price == pytest.approx(expected, abs=1e-6)
    assert q.meta["levels_used"] == 3 and q.meta["worst_price"] == 1.1225 and q.meta["top_of_book"] == 1.1230
    assert q.meta["pool"] == "order book, 3 levels" and q.meta["book_time"] == "2026-10-05T02:40:00Z"
    assert f.calls == [("EURC-USDC", 500)]


def test_buy_side_and_one_fetch_for_both_sides():
    f = Fetcher()
    venue = coinbase.CoinbaseOrderBook(fetcher=f)
    sell = venue.quote(pair(), "sell", 100_000, 1.1232)
    buy = venue.quote(pair(), "buy", 300_000, 1.1232)  # spends 300k x 1.1232 USDC
    assert sell.price == pytest.approx(1.1230, abs=1e-6)
    spend = 300_000 * 1.1232
    eurc = 250_000 + (spend - 250_000 * 1.1234) / 1.1238
    assert buy.quote_amount == pytest.approx(spend) and buy.base_amount == pytest.approx(eurc, abs=1e-3)
    assert buy.price > 1.1234 and buy.meta["levels_used"] == 2
    assert len(f.calls) == 1  # the book is reused within the request


def test_book_refetched_after_ttl():
    t = [0.0]
    f = Fetcher()
    venue = coinbase.CoinbaseOrderBook(fetcher=f, clock=lambda: t[0])
    venue.quote(pair(), "sell", 1000, 1.12)
    t[0] = 5.0
    venue.quote(pair(), "sell", 1000, 1.12)
    assert len(f.calls) == 2


def test_taker_fee_reduces_proceeds():
    q = coinbase.CoinbaseOrderBook(fetcher=Fetcher()).quote(pair(taker_fee_bps=10), "sell", 1000, 1.12)
    assert q.price == pytest.approx(1.1230 * 0.999, abs=1e-6) and q.meta["taker_fee_bps"] == 10


def test_not_enough_depth_is_a_clear_failure():
    with pytest.raises(VenueError, match=r"only cover 1,200,000 EURC of the 2,000,000"):
        coinbase.CoinbaseOrderBook(fetcher=Fetcher()).quote(pair(), "sell", 2_000_000, 1.12)


def test_empty_book_and_missing_product():
    empty = Fetcher({"pricebook": {"bids": [], "asks": []}})
    with pytest.raises(VenueError, match="no bids"):
        coinbase.CoinbaseOrderBook(fetcher=empty).quote(pair(), "sell", 1000, 1.12)
    bare = Pair(base=EURC, quote=USDC, reference="EUR/USD")
    assert not coinbase.CoinbaseOrderBook(fetcher=Fetcher()).supports(bare)
    assert coinbase.CoinbaseOrderBook(fetcher=Fetcher()).supports(pair())


def test_custom_depth_limit_is_requested():
    f = Fetcher()
    coinbase.CoinbaseOrderBook(fetcher=f).quote(pair(depth_limit=1000), "sell", 1000, 1.12)
    assert f.calls == [("EURC-USDC", 1000)]


# --- fetching -----------------------------------------------------------------


def test_fetch_uses_sdk_with_keys(monkeypatch):
    seen = {}

    class FakeResp:
        def to_dict(self):
            return BOOK

    class FakeClient:
        def __init__(self, api_key, api_secret):
            seen["key"], seen["secret"] = api_key, api_secret

        def get_product_book(self, product_id, limit):
            seen["args"] = (product_id, limit)
            return FakeResp()

    rest = types.ModuleType("coinbase.rest")
    rest.RESTClient = FakeClient
    monkeypatch.setitem(sys.modules, "coinbase", types.ModuleType("coinbase"))
    monkeypatch.setitem(sys.modules, "coinbase.rest", rest)
    monkeypatch.setenv("COINBASE_API_KEY", "organizations/o/apiKeys/k")
    monkeypatch.setenv("COINBASE_API_SECRET", "-----BEGIN EC PRIVATE KEY-----\\nabc\\n-----END EC PRIVATE KEY-----")
    assert coinbase.fetch_book("EURC-USDC", 500) == BOOK
    assert seen["args"] == ("EURC-USDC", 500) and seen["key"] == "organizations/o/apiKeys/k"
    assert "\n" in seen["secret"] and "\\n" not in seen["secret"]  # literal \n turned into real newlines


def test_fetch_falls_back_to_public_endpoint(monkeypatch):
    import urllib.request

    monkeypatch.delenv("COINBASE_API_KEY", raising=False)
    monkeypatch.delenv("COINBASE_API_SECRET", raising=False)
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["url"], seen["ua"] = req.full_url, req.get_header("User-agent")
        return Resp(json.dumps(BOOK).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert coinbase.fetch_book("EURC-USDC", 500) == BOOK
    assert seen["url"].startswith(coinbase.PUBLIC_BOOK_URL) and "product_id=EURC-USDC" in seen["url"] and "limit=500" in seen["url"]
    assert seen["ua"].startswith("execution-cost-tracker")


# --- config ---------------------------------------------------------------------


def test_shipped_config_enables_coinbase_for_eurc_usdc():
    p = registry.load().pair("EURC/USDC", "base")
    assert p.params_for("coinbase")["product_id"] == "EURC-USDC"


@pytest.mark.parametrize(
    "cb,match",
    [
        ({"product_id": "eurc usdc"}, "product_id"),
        ({"product_id": "EURC-USDC", "depth_limit": 0}, "depth_limit"),
        ({"product_id": "EURC-USDC", "depth_limit": "lots"}, "depth_limit"),
        ({"product_id": "EURC-USDC", "taker_fee_bps": -1}, "taker_fee_bps"),
        ("EURC-USDC", "mapping"),
    ],
)
def test_bad_coinbase_settings_rejected(cb, match):
    raw = {
        "chains": {"base": {"chain_id": 8453, "tokens": {
            "USDC": {"address": USDC.address, "decimals": 6}, "EURC": {"address": EURC.address, "decimals": 6}}}},
        "pairs": [{"base": "EURC", "quote": "USDC", "reference": "EUR/USD", "chains": ["base"],
                   "venue_params": {"coinbase": cb}}],
    }
    with pytest.raises(registry.RegistryError, match=match):
        registry.parse(raw)
