"""Rate limiting, caching and the guarded quote path, with a fake clock."""

import pytest

from execution_cost_tracker import registry, service
from execution_cost_tracker.guardrails import RateLimited, SlidingWindowLimiter, TTLCache, client_key
from execution_cost_tracker.reference import StaticReference
from execution_cost_tracker.venues import SwapResult, Venue, VenueError


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_sliding_window_limits_and_recovers():
    clock = Clock()
    lim = SlidingWindowLimiter(limit=2, window=60, clock=clock)
    for _ in range(2):
        assert lim.check("a") == 0
        lim.record("a")
    assert lim.check("a") == pytest.approx(60)
    assert lim.check("b") == 0  # other keys unaffected
    clock.t += 30
    assert lim.check("a") == pytest.approx(30)
    clock.t += 30.01
    assert lim.check("a") == 0


def test_limiter_forgets_old_keys():
    lim = SlidingWindowLimiter(limit=1, window=60, max_keys=2, clock=Clock())
    for k in "abc":
        lim.record(k)
    assert lim.check("a") == 0 and lim.check("c") > 0


def test_ttl_cache_expires_and_caps():
    clock = Clock()
    cache = TTLCache(ttl=10, max_entries=2, clock=clock)
    cache.set("x", 1)
    clock.t += 4
    assert cache.get("x") == 1 and cache.age("x") == pytest.approx(4)
    clock.t += 6
    assert cache.get("x") is None and cache.age("x") is None
    for k in "abc":
        cache.set(k, k)
    assert cache.get("a") is None and cache.get("c") == "c"
    off = TTLCache(ttl=0)
    off.set("x", 1)
    assert off.get("x") is None


def test_client_key_prefers_vercel_headers():
    assert client_key({"X-Real-IP": "1.1.1.1", "x-forwarded-for": "2.2.2.2"}) == "1.1.1.1"
    assert client_key({"X-Forwarded-For": "2.2.2.2, 10.0.0.1"}) == "2.2.2.2"
    assert client_key({}, "3.3.3.3") == "3.3.3.3"
    assert client_key({}) == "unknown"


def test_rate_limited_message():
    e = RateLimited("client", 4.2)
    assert e.retry_after == 5 and "you" in str(e) and "5s" in str(e)
    assert "everyone" in str(RateLimited("global", 0.1)) and RateLimited("global", 0.1).retry_after == 1


# --- guarded_quote -------------------------------------------------------------


class Fake(Venue):
    def __init__(self, name, fail=False):
        self.name, self.fail = name, fail
        self.calls = 0

    def simulate(self, pair, token_in, token_out, amount_in):
        self.calls += 1
        if self.fail:
            raise VenueError("no pool")
        amount = token_in.from_raw(amount_in)
        out = amount * 1.084 if token_in is pair.base else amount / 1.086
        return SwapResult(token_out.to_raw(out))


REG = registry.parse(
    {
        "chains": {
            "base": {
                "chain_id": 8453,
                "tokens": {
                    "USDC": {"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
                    "EURC": {"address": "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42", "decimals": 6},
                },
            }
        },
        "pairs": [{"base": "EURC", "quote": "USDC", "reference": "EUR/USD", "chains": ["base"]}],
        "guardrails": {"per_client_per_minute": 2, "global_per_minute": 3, "cache_seconds": 10},
    }
)


def setup(fail=False):
    clock = Clock()
    guard = service.Guard.from_registry(REG, clock=clock)
    venue = Fake("zerox", fail=fail)
    kw = {"reference": StaticReference({"EUR/USD": 1.085}), "venue_factory": lambda names: [venue]}
    return clock, guard, venue, kw


def params(notional=1000):
    return service.QuoteParams(notional=notional, venues=["zerox"], registry=REG)


def test_identical_requests_hit_cache_without_counting():
    clock, guard, venue, kw = setup()
    first = service.guarded_quote(params(), "a", guard=guard, **kw)
    assert first["cached"] is False and venue.calls == 2
    clock.t += 3
    for _ in range(5):  # far past the per-client limit, but all cache hits
        again = service.guarded_quote(params(), "a", guard=guard, **kw)
    assert again["cached"] is True and again["cache_age_seconds"] == pytest.approx(3)
    assert venue.calls == 2 and again["quotes"] == first["quotes"]
    clock.t += 8  # cache expired
    assert service.guarded_quote(params(), "a", guard=guard, **kw)["cached"] is False


def test_per_client_and_global_limits():
    clock, guard, venue, kw = setup()
    service.guarded_quote(params(1000), "a", guard=guard, **kw)
    service.guarded_quote(params(2000), "a", guard=guard, **kw)
    with pytest.raises(RateLimited) as e:
        service.guarded_quote(params(3000), "a", guard=guard, **kw)
    assert e.value.scope == "client"
    service.guarded_quote(params(3000), "b", guard=guard, **kw)  # third overall
    with pytest.raises(RateLimited) as e:
        service.guarded_quote(params(4000), "c", guard=guard, **kw)
    assert e.value.scope == "global"
    clock.t += 61
    assert service.guarded_quote(params(4000), "c", guard=guard, **kw)["quotes"]


def test_total_failures_are_not_cached():
    clock, guard, venue, kw = setup(fail=True)
    out = service.guarded_quote(params(), "a", guard=guard, **kw)
    assert out["quotes"] == [] and out["errors"]
    service.guarded_quote(params(), "a", guard=guard, **kw)
    assert venue.calls == 4  # second request re-quoted rather than served from cache
