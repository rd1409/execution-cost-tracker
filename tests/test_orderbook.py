"""Order-book walk: worked examples you can check by hand."""

import pytest

from execution_cost_tracker import orderbook as ob

BIDS = ob.parse_levels(
    [{"price": "1.1230", "size": "300000"}, {"price": "1.1228", "size": "500000"}, {"price": "1.1225", "size": "400000"}],
    "bids",
)
ASKS = ob.parse_levels(
    [["1.1238", "200000"], ["1.1234", "250000"], ["1.1241", "1000000"]],  # unsorted on purpose
    "asks",
)


def test_parse_sorts_best_first_and_skips_junk():
    assert [lv.price for lv in BIDS] == [1.1230, 1.1228, 1.1225]
    assert [lv.price for lv in ASKS] == [1.1234, 1.1238, 1.1241]
    junk = ob.parse_levels([{"price": "x", "size": "1"}, {"price": "1.1", "size": "0"}, {"size": "5"}, ["1.2", "3"]], "bids")
    assert junk == [ob.Level(1.2, 3.0)]
    with pytest.raises(ValueError):
        ob.parse_levels([], "middle")


def test_sell_one_million_eurc_walks_three_levels():
    fill = ob.sell_base(BIDS, 1_000_000)
    # 300k @ 1.1230 + 500k @ 1.1228 + 200k @ 1.1225
    expected_quote = 300_000 * 1.1230 + 500_000 * 1.1228 + 200_000 * 1.1225
    assert fill.complete and fill.levels_used == 3
    assert fill.base_filled == pytest.approx(1_000_000)
    assert fill.quote_amount == pytest.approx(expected_quote)
    assert fill.avg_price == pytest.approx(expected_quote / 1_000_000)  # 1.12279
    assert fill.best_price == 1.1230 and fill.worst_price == 1.1225
    assert fill.slippage_vs_best_bps == pytest.approx((1.1230 - fill.avg_price) / 1.1230 * 1e4)


def test_sell_inside_top_level_is_top_price():
    fill = ob.sell_base(BIDS, 1000)
    assert fill.levels_used == 1 and fill.avg_price == pytest.approx(1.1230) and fill.slippage_vs_best_bps == 0


def test_exact_level_boundary_is_complete():
    fill = ob.sell_base(BIDS, 800_000)
    assert fill.complete and fill.levels_used == 2


def test_buy_base_lifts_asks():
    fill = ob.buy_base(ASKS, 300_000)
    expected_quote = 250_000 * 1.1234 + 50_000 * 1.1238
    assert fill.complete and fill.levels_used == 2
    assert fill.quote_amount == pytest.approx(expected_quote)
    assert fill.avg_price == pytest.approx(expected_quote / 300_000)
    assert fill.slippage_vs_best_bps > 0


def test_buy_with_quote_spends_exact_usdc():
    spend = 250_000 * 1.1234 + 100_000 * 1.1238  # takes all of level 1, 100k of level 2
    fill = ob.buy_with_quote(ASKS, spend)
    assert fill.complete and fill.levels_used == 2
    assert fill.quote_amount == pytest.approx(spend)
    assert fill.base_filled == pytest.approx(350_000)


def test_not_enough_depth_is_reported():
    fill = ob.sell_base(BIDS, 2_000_000)  # only 1.2M of bids
    assert not fill.complete and fill.base_filled == pytest.approx(1_200_000) and fill.levels_used == 3


def test_bad_inputs():
    with pytest.raises(ValueError):
        ob.sell_base(BIDS, 0)
    with pytest.raises(ValueError):
        ob.sell_base(BIDS, float("nan"))
    with pytest.raises(ValueError, match="no bids"):
        ob.sell_base([], 10)


def test_fee():
    sell = ob.sell_base(BIDS, 1000)
    base, quote = ob.apply_fee(sell, 10)  # 10 bps
    assert base == 1000 and quote == pytest.approx(1000 * 1.1230 * 0.999)
    buy = ob.buy_base(ASKS, 1000)
    base, quote = ob.apply_fee(buy, 10)
    assert base == pytest.approx(999) and quote == pytest.approx(1000 * 1.1234)
