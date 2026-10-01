import pytest

from execution_cost_tracker import Quote, Side
from execution_cost_tracker import metrics


def q(side, base, quote_amt, venue="v"):
    return Quote(venue=venue, pair="EURC/USDC", chain="base", side=side, base_amount=base, quote_amount=quote_amt)


def test_quote_price_is_quote_per_base():
    assert q("sell", 1000, 1085).price == pytest.approx(1.085)


def test_bps():
    assert metrics.bps(1, 100) == pytest.approx(100)
    with pytest.raises(ValueError):
        metrics.bps(1, 0)


def test_deviation_buy_above_mid_is_cost():
    assert metrics.deviation_bps(1.0860, 1.0850, "buy") == pytest.approx(0.001 / 1.085 * 10_000)


def test_deviation_sell_below_mid_is_cost():
    assert metrics.deviation_bps(1.0840, 1.0850, Side.SELL) == pytest.approx(0.001 / 1.085 * 10_000)


def test_deviation_price_improvement_is_negative():
    assert metrics.deviation_bps(1.0860, 1.0850, "sell") < 0
    assert metrics.deviation_bps(1.0840, 1.0850, "buy") < 0


def test_quote_deviation_matches_deviation():
    quote = q("buy", 1000, 1086)
    assert metrics.quote_deviation_bps(quote, 1.085) == pytest.approx(metrics.deviation_bps(1.086, 1.085, "buy"))


def test_quoted_spread_and_midpoint():
    assert metrics.midpoint(1.084, 1.086) == pytest.approx(1.085)
    assert metrics.quoted_spread_bps(1.084, 1.086) == pytest.approx(0.002 / 1.085 * 10_000)
    assert metrics.quoted_spread_bps(1.084, 1.086, mid=1.0) == pytest.approx(20)


def test_mid_offset_premium_positive():
    assert metrics.mid_offset_bps(1.086, 1.085) > 0
    assert metrics.mid_offset_bps(1.085, 1.085) == 0


def test_best_quote_by_side():
    sells = [q("sell", 1000, 1084, "a"), q("sell", 1000, 1085, "b")]
    buys = [q("buy", 1000, 1086, "a"), q("buy", 1000, 1087, "b")]
    assert metrics.best_quote(sells).venue == "b"
    assert metrics.best_quote(buys).venue == "a"
    assert metrics.best_quote([]) is None
    with pytest.raises(ValueError):
        metrics.best_quote([sells[0], buys[0]])


def test_gas_cost_bps():
    # 200k gas at 0.01 gwei with ETH at $3000 = $0.006 on $1000 notional = 0.06 bps
    assert metrics.gas_cost_bps(200_000, 10**7, 3000, 1000) == pytest.approx(0.06)


def test_quote_validates_amounts():
    with pytest.raises(ValueError):
        q("sell", 0, 1)
