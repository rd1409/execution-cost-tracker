"""Offline tests for venues' shared logic, reference sources, storage and run."""

import io

import pytest

from execution_cost_tracker import Pair, Side, Token, storage
from execution_cost_tracker.reference import FrankfurterReference, StaticReference
from execution_cost_tracker.run import print_report, run_cycle
from execution_cost_tracker.venues import SwapResult, Venue, VenueError, ZeroX
from execution_cost_tracker.venues.base import best_of

USDC = Token("USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6)
EURC = Token("EURC", "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42", 6)
PAIR = Pair(base=EURC, quote=USDC, reference="EUR/USD")


class FakeVenue(Venue):
    """Fills at a fixed bid/ask around a mid."""

    def __init__(self, name, bid, ask):
        self.name, self.bid, self.ask = name, bid, ask

    def simulate(self, pair, token_in, token_out, amount_in):
        amount = token_in.from_raw(amount_in)
        out = amount * self.bid if token_in is pair.base else amount / self.ask
        return SwapResult(token_out.to_raw(out), 150_000, {"fake": True})


class BrokenVenue(Venue):
    name = "broken"

    def simulate(self, pair, token_in, token_out, amount_in):
        raise VenueError("no pool")


def test_token_raw_round_trip():
    assert EURC.to_raw(1.5) == 1_500_000
    assert EURC.from_raw(1_500_000) == 1.5


def test_venue_quote_both_sides():
    v = FakeVenue("fake", bid=1.084, ask=1.086)
    sell = v.quote(PAIR, Side.SELL, 1000, mid_hint=1.085)
    buy = v.quote(PAIR, "buy", 1000, mid_hint=1.085)
    assert sell.base_amount == 1000 and sell.price == pytest.approx(1.084)
    assert buy.quote_amount == pytest.approx(1085) and buy.price == pytest.approx(1.086, rel=1e-5)
    assert sell.pair == "EURC/USDC" and sell.gas_estimate == 150_000


def test_best_of_picks_largest_and_reports_failures():
    def boom():
        raise RuntimeError("revert")

    r = best_of([("a", lambda: SwapResult(10)), ("b", boom), ("c", lambda: SwapResult(12))], "x")
    assert r.amount_out == 12 and r.meta["pool"] == "c"
    with pytest.raises(VenueError, match="revert"):
        best_of([("b", boom)], "x")


def test_static_reference_handles_inverse():
    ref = StaticReference({"EUR/USD": 1.25})
    assert ref.mid("EUR/USD").mid == 1.25
    assert ref.mid("usd/eur").mid == pytest.approx(0.8)


def test_frankfurter_parses_response():
    seen = {}

    def fake_get(url):
        seen["url"] = url
        return {"amount": 1.0, "base": "EUR", "date": "2026-09-30", "rates": {"USD": 1.0851}}

    rate = FrankfurterReference(http_get=fake_get).mid("EUR/USD")
    assert rate.mid == 1.0851 and rate.as_of == "2026-09-30"
    assert "base=EUR" in seen["url"] and "symbols=USD" in seen["url"]


def test_zerox_parses_price_response():
    calls = {}

    def fake_get(url, headers):
        calls["url"], calls["headers"] = url, headers
        return {
            "liquidityAvailable": True,
            "buyAmount": "1084000000",
            "sellAmount": "1000000000",
            "gas": "180000",
            "route": {"fills": [{"source": "Uniswap_V3"}, {"source": "Aerodrome_V3"}]},
            "fees": {"zeroExFee": None},
        }

    q = ZeroX(api_key="k", http_get=fake_get).quote(PAIR, "sell", 1000, 1.085)
    assert q.price == pytest.approx(1.084)
    assert q.gas_estimate == 180_000
    assert q.meta["sources"] == ["Aerodrome_V3", "Uniswap_V3"]
    assert calls["headers"] == {"0x-api-key": "k", "0x-version": "v2"}
    assert "chainId=8453" in calls["url"] and "sellAmount=1000000000" in calls["url"]


def test_zerox_without_key_fails(monkeypatch):
    monkeypatch.delenv("ZEROX_API_KEY", raising=False)
    with pytest.raises(VenueError):
        ZeroX(http_get=lambda u, h: {}).quote(PAIR, "sell", 1000, 1.085)


def test_run_cycle_scores_stores_and_prints(tmp_path):
    conn = storage.connect(tmp_path / "fx.db")
    venues = [FakeVenue("tight", 1.0849, 1.0851), FakeVenue("wide", 1.0840, 1.0860), BrokenVenue()]
    result = run_cycle([PAIR], venues, StaticReference({"EUR/USD": 1.085}), [1000, 10000], conn, run_id="r1")

    assert len(result.rows) == 2 * 2 * 2  # venues x sizes x sides
    assert len(result.failures) == 4 and {f.venue for f in result.failures} == {"broken"}

    spreads = {(s["venue"], s["size"]): s for s in result.spreads()}
    tight, wide = spreads[("tight", 1000)], spreads[("wide", 1000)]
    assert tight["spread_bps"] == pytest.approx(0.0002 / 1.085 * 10_000, rel=1e-4)
    assert wide["spread_bps"] > tight["spread_bps"]
    assert wide["sell_cost_bps"] > 0 and wide["buy_cost_bps"] > 0
    assert abs(wide["mid_offset_bps"]) < 0.01

    stored = storage.load_quotes(conn, run_id="r1")
    assert len(stored) == 8 and stored[0]["meta"] == {"fake": True}
    assert len(storage.load_quotes(conn, venue="tight")) == 4
    assert len(storage.load_table(conn, "failures", "r1")) == 4
    assert storage.load_table(conn, "reference_rates")[0]["mid"] == 1.085

    out = io.StringIO()
    print_report(result, out)
    text = out.getvalue()
    assert "EURC/USDC" in text and "tight" in text and "broken x4" in text


def test_run_cycle_reference_failure_skips_pair():
    result = run_cycle([PAIR], [FakeVenue("v", 1, 1)], StaticReference({}), [1000])
    assert result.rows == [] and result.failures[0].venue == "reference"


def test_run_cycle_single_side():
    result = run_cycle([PAIR], [FakeVenue("v", 1.084, 1.086)], StaticReference({"EUR/USD": 1.085}), [1000], sides=["buy"])
    assert [r.quote.side for r in result.rows] == [Side.BUY]
    spread = result.spreads()[0]
    assert spread["bid"] is None and spread["ask"] is not None and spread["spread_bps"] is None


def test_default_pairs_and_sizes_come_from_config():
    from execution_cost_tracker import registry
    from execution_cost_tracker.run import default_pairs, default_sizes

    reg = registry.get()
    assert default_pairs() == list(reg.pairs) and default_sizes() == list(reg.cron_sizes)
