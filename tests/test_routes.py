"""Route planner: start token/chain -> venue -> end token/chain, all offline."""

import json
from datetime import datetime, timezone

import pytest

from execution_cost_tracker import registry, service
from execution_cost_tracker.bridges import Bridge, BridgeError, BridgeQuote
from execution_cost_tracker.gas import GasOracle
from execution_cost_tracker.routes import COINBASE, RoutePlanner, RouteRequest
from execution_cost_tracker.venues import SwapResult, Venue, VenueError

REG = registry.get()
MID = 1.10


class FakeVenue(Venue):
    """Sells EURC at ``bid`` and buys at ``ask`` (USDC per EURC)."""

    def __init__(self, name, chains=("base",), bid=1.099, ask=1.101, gas=120_000):
        self.name, self.chains, self.bid, self.ask, self.gas = name, set(chains), bid, ask, gas

    def supports(self, pair):
        return self.name == "coinbase" or pair.chain in self.chains

    def simulate(self, pair, token_in, token_out, amount_in):
        x = token_in.from_raw(amount_in)
        out = x * self.bid if token_in.symbol == "EURC" else x / self.ask
        return SwapResult(token_out.to_raw(out), None if self.name == "coinbase" else self.gas, {"pool": "fake"})


class FakeBridge(Bridge):
    """Takes ``fee_bps`` of the amount; optionally reports its own gas in USD."""

    def __init__(self, name, fee_bps=5.0, gas_usd=None, fail=None, label=None):
        self.name, self.fee_bps, self.gas_usd, self.fail = name, fee_bps, gas_usd, fail
        self.label = label or name.title()
        self.calls = []

    def quote(self, reg, token, a, b, amount):
        self.calls.append((token, a, b, round(amount, 6)))
        if self.fail:
            raise BridgeError(f"{self.label}: {self.fail}")
        return BridgeQuote(self.name, token, a, b, amount, amount * (1 - self.fee_bps / 1e4),
                           origin_gas_usd=self.gas_usd, gas_units=None if self.gas_usd is not None else 200_000,
                           eta_seconds=10, route=f"{self.name} fill")


def oracle(gwei=1.0, eth=3000.0, pol=0.5, sol=150.0):
    prices = {"ETH-USD": eth, "POL-USD": pol, "SOL-USD": sol}
    return GasOracle(
        REG,
        http_get=lambda url, h: {"price": str(prices[url.rsplit("/", 1)[1]])},
        http_post=lambda url, body, h: {"result": hex(int(gwei * 1e9))},
    )


def planner(venues=None, bridges=None, gas=None, **kw):
    venues = venues or {
        "coinbase": FakeVenue("coinbase", bid=1.0995, ask=1.1005),
        "uniswap_v3": FakeVenue("uniswap_v3", chains=("base", "ethereum")),
        "aerodrome": FakeVenue("aerodrome", chains=("base",), bid=1.0985, ask=1.1015),
    }
    bridges = bridges if bridges is not None else [FakeBridge("across", 4), FakeBridge("relay", 3, gas_usd=0.02)]
    return RoutePlanner(REG, venues, bridges, gas or oracle(), MID, venue_labels=service.VENUE_LABELS, **kw)


def req(ft="EURC", fc="base", tt="USDC", tc="base", amount=100_000, venues=("coinbase", "uniswap_v3", "aerodrome")):
    return RouteRequest(ft, fc, tt, tc, amount, list(venues))


def by(plan, venue, place):
    return next(r for r in plan.routes if r["venue"] == venue and r["place"] == place)


def test_same_chain_routes_and_coinbase_deposit_withdraw():
    plan = planner().plan(req())
    places = {(r["venue"], r["place"]) for r in plan.routes}
    assert places == {("coinbase", COINBASE), ("uniswap_v3", "base"), ("aerodrome", "base")}

    cb = by(plan, "coinbase", COINBASE)
    assert [leg["kind"] for leg in cb["legs"]] == ["deposit", "swap", "withdraw"]
    dep, trade, wd = cb["legs"]
    assert dep["signer_chain"] == "base" and dep["gas"]["gas_token"] == "ETH" and dep["amount_out"] == 100_000
    assert trade["amount_out"] == pytest.approx(109_950) and trade["signer_chain"] is None
    # Coinbase's send fee: transfer gas (65k x 1 gwei x $3000) taken from the USDC
    assert wd["meta"]["send_fee_basis"] == "network estimate"
    assert wd["amount_out"] == pytest.approx(109_950 - 65_000e-9 * 3000)
    assert cb["gas_usd"] == pytest.approx(65_000e-9 * 3000)  # only the deposit is signed by you
    assert [g["chain"] for g in cb["gas_tokens"]] == ["base"]

    uni = by(plan, "uniswap_v3", "base")
    assert [leg["kind"] for leg in uni["legs"]] == ["swap"]
    assert uni["legs"][0]["gas"]["units"] == 120_000 + 50_000  # venue estimate + approval

    # Uniswap on Ethereum would need an EURC bridge, which no configured bridge offers
    fail = next(f for f in plan.failures if f["place"] == "ethereum")
    assert fail["venue"] == "uniswap_v3" and "no configured bridge moves EURC" in fail["error"]

    bd = cb["cost_breakdown_bps"]
    assert bd["trade"] == pytest.approx((1.10 - 1.0995) / 1.10 * 1e4)
    assert bd["transfers"] == pytest.approx(65_000e-9 * 3000 / 110_000 * 1e4)  # Coinbase's send fee
    assert bd["total"] == pytest.approx(cb["all_in_cost_bps"])  # parts add up to the all-in cost

    assert plan.routes[0]["venue"] == "coinbase"  # best net of gas first
    assert plan.routes[0]["net_amount_out"] >= plan.routes[-1]["net_amount_out"]


def test_rates_and_cost_are_in_quote_per_base():
    plan = planner().plan(req(ft="USDC", tt="EURC", amount=110_000))
    uni = by(plan, "uniswap_v3", "base")
    assert uni["side"] == "buy" and uni["trade_price"] == pytest.approx(1.101)
    assert uni["effective_rate"] == pytest.approx(110_000 / uni["amount_out"])
    assert uni["all_in_rate"] > uni["effective_rate"]  # gas makes buying dearer
    assert uni["all_in_cost_bps"] > 0


def test_tempo_usdc_bridges_into_coinbase_via_cheapest_hub():
    across = FakeBridge("across", 4)
    relay = FakeBridge("relay", 3, gas_usd=0.02)
    plan = planner(bridges=[across, relay]).plan(req(ft="USDC", fc="tempo", tt="EURC", tc="base", amount=50_000))
    cb = by(plan, "coinbase", COINBASE)
    first = cb["legs"][0]
    assert first["kind"] == "bridge" and first["to"] == COINBASE and first["provider"] == "relay"
    assert first["meta"]["deposit_network"] in ("base", "arbitrum", "ethereum")
    assert "straight to your Coinbase deposit address" in first["description"]
    assert "USDC.e" in first["description"] and "Tempo" in first["description"]
    alts = {a["provider"]: a for a in first["alternatives"]}
    assert set(alts) == {"across", "relay"} and alts["relay"]["chosen"]
    tempo = next(g for g in cb["gas_tokens"] if g["chain"] == "tempo")
    assert tempo["gas_token"] == "USD stablecoin" and "EURC.e can't pay fees" in tempo["note"]
    assert cb["legs"][-1]["kind"] == "withdraw" and cb["legs"][-1]["to"] == "base"
    # the same bridge quote is reused across venues within one request
    assert len(set(across.calls)) == len(across.calls)


def test_coinbase_withdraws_to_hub_then_bridges_when_end_chain_not_supported():
    plan = planner().plan(req(ft="EURC", fc="base", tt="USDC", tc="tempo", amount=20_000))
    cb = by(plan, "coinbase", COINBASE)
    kinds = [leg["kind"] for leg in cb["legs"]]
    assert kinds == ["deposit", "swap", "withdraw", "bridge"]
    wd, br = cb["legs"][2:]
    assert wd["to"] == br["from"] and br["to"] == "tempo"
    assert {g["chain"] for g in cb["gas_tokens"]} == {"base", wd["to"]}
    # DEX on Base bridges its USDC output straight to Tempo
    uni = by(plan, "uniswap_v3", "base")
    assert [leg["kind"] for leg in uni["legs"]] == ["swap", "bridge"]


def test_eurc_to_tempo_has_no_bridge_and_says_so():
    plan = planner().plan(req(ft="USDC", fc="base", tt="EURC", tc="tempo", amount=1_000))
    assert plan.routes == []
    errors = " ".join(f["error"] for f in plan.failures)
    assert "EURC" in errors and "Tempo" in errors


def test_bridge_errors_are_listed_and_others_still_used():
    bad = FakeBridge("across", fail="route not supported")
    plan = planner(bridges=[bad, FakeBridge("relay", 3, gas_usd=0.02)]).plan(
        req(ft="USDC", fc="arbitrum", tt="EURC", tc="base", amount=10_000)
    )
    uni = by(plan, "uniswap_v3", "base")
    br = uni["legs"][0]
    assert br["provider"] == "relay"
    assert any("route not supported" in a.get("error", "") for a in br["alternatives"])


def test_all_bridges_failing_fails_the_route_only():
    plan = planner(bridges=[FakeBridge("across", fail="down"), FakeBridge("relay", fail="down too")]).plan(
        req(ft="USDC", fc="polygon", tt="EURC", tc="base", amount=10_000)
    )
    assert plan.routes == [] or all(r["venue"] == "coinbase" for r in plan.routes)
    assert any("no bridge quoted USDC from Polygon to Base" in f["error"] for f in plan.failures)


def test_solana_and_polygon_gas_tokens():
    plan = planner().plan(req(ft="EURC", fc="solana", tt="USDC", tc="polygon", amount=5_000))
    cb = by(plan, "coinbase", COINBASE)
    dep = cb["legs"][0]
    assert dep["kind"] == "deposit" and dep["gas"]["gas_token"] == "SOL"
    assert dep["gas"]["usd"] == pytest.approx(REG.routing.solana_fee_lamports / 1e9 * 150)
    assert cb["legs"][-1]["kind"] == "withdraw" and cb["legs"][-1]["to"] == "polygon"
    assert cb["legs"][-1]["meta"]["send_fee"] == pytest.approx(65_000e-9 * 0.5)  # POL valued at $0.50


def test_venue_failure_and_band():
    class Broken(FakeVenue):
        def simulate(self, *a):
            raise VenueError("pool reverted")

    venues = {"uniswap_v3": Broken("uniswap_v3"), "aerodrome": FakeVenue("aerodrome", bid=1.05, ask=1.15)}
    plan = planner(venues=venues).plan(req(venues=("uniswap_v3", "aerodrome")))
    assert plan.routes == []
    assert plan.excluded[0]["venue"] == "aerodrome" and plan.excluded[0]["offset_pct"] < -2
    assert any("pool reverted" in f["error"] and f["stage"] == "trade" for f in plan.failures)


def test_gas_price_unavailable_is_flagged_not_fatal():
    def down(url, body, h):
        raise OSError("rpc down")

    gas = GasOracle(REG, http_get=lambda u, h: {"price": "3000"}, http_post=down)
    plan = planner(gas=gas).plan(req(venues=("uniswap_v3",)))
    uni = plan.routes[0]
    assert uni["legs"][0]["gas"] is None and "gas price unavailable" in uni["legs"][0]["gas_error"]
    assert uni["gas_incomplete"]


# --- service layer ------------------------------------------------------------


def test_route_params_validation():
    with pytest.raises(ValueError, match="EURC isn't available on Arbitrum"):
        service.RouteParams(from_token="EURC", from_chain="arbitrum")
    with pytest.raises(ValueError, match="two different stablecoins"):
        service.RouteParams(from_token="USDC", to_token="USDC")
    with pytest.raises(ValueError, match="unknown start chain"):
        service.RouteParams(from_chain="optimism")
    with pytest.raises(ValueError, match="amount must be between"):
        service.RouteParams(amount=0)
    with pytest.raises(ValueError, match="unknown venue"):
        service.RouteParams(venues=["nope"])
    p = service.RouteParams(from_token="USDC", from_chain="tempo", to_token="EURC", to_chain="solana", amount=5000)
    assert "coinbase" in p.venues and "aerodrome" in p.venues
    assert p.cache_key()[0] == "route"


def _mid_row(mid=1.10):
    return {"mid": mid, "source": "tradingview", "received_at": datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc),
            "bid": None, "ask": None}


def test_service_route_end_to_end_json_and_market_mid():
    params = service.RouteParams(from_token="EURC", from_chain="base", to_token="USDC", to_chain="arbitrum",
                                 amount=10_000, venues=["coinbase", "uniswap_v3", "aerodrome"])
    venues = {"coinbase": FakeVenue("coinbase", bid=1.0995), "uniswap_v3": FakeVenue("uniswap_v3", chains=("base", "ethereum")),
              "aerodrome": FakeVenue("aerodrome")}
    out = service.route(params, venues=venues, bridges=[FakeBridge("across", 4)], gas=oracle(),
                        mid_lookup=lambda pair: _mid_row(), now=datetime(2026, 10, 5, 14, 1, tzinfo=timezone.utc))
    json.dumps(out)
    assert out["side"] == "sell" and out["reference"]["mid"] == 1.10
    assert out["market_mid"]["available"]  # Monday 14:01 UTC, price 1 minute old
    best = out["best"]
    assert best["vs_market_mid_bps"] == pytest.approx((1.10 - best["all_in_rate"]) / 1.10 * 1e4)
    cb = next(r for r in out["routes"] if r["venue"] == "coinbase")
    assert cb["legs"][-1]["kind"] == "withdraw" and cb["legs"][-1]["to"] == "arbitrum"  # Coinbase sends USDC on Arbitrum
    aero = next(r for r in out["routes"] if r["venue"] == "aerodrome")
    assert aero["legs"][-1]["kind"] == "bridge" and aero["legs"][-1]["to"] == "arbitrum"


def test_service_route_outside_fx_hours_shows_na():
    params = service.RouteParams(venues=["uniswap_v3"])
    out = service.route(params, venues={"uniswap_v3": FakeVenue("uniswap_v3")}, bridges=[], gas=oracle(),
                        mid_lookup=lambda pair: _mid_row(), now=datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc))
    assert out["market_mid"]["available"] is False
    assert all(r["vs_market_mid_bps"] is None for r in out["routes"])


def test_guarded_route_caches(monkeypatch):
    calls = []

    def fake_route(params, **kw):
        calls.append(1)
        return {"routes": [{"venue": "x"}]}

    monkeypatch.setattr(service, "route", fake_route)
    guard = service.Guard.from_registry(REG)
    p = service.RouteParams()
    first = service.guarded_route(p, "1.2.3.4", guard)
    again = service.guarded_route(service.RouteParams(), "1.2.3.4", guard)
    assert not first["cached"] and again["cached"] and len(calls) == 1
