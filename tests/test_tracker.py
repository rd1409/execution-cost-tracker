from datetime import datetime, timezone

import pytest

from execution_cost_tracker import Execution, ExecutionCostTracker, Side


def ts(day: int) -> datetime:
    return datetime(2026, 9, day, 14, 30, tzinfo=timezone.utc)


def test_buy_slippage_is_positive_when_paying_up():
    e = Execution("AAPL", Side.BUY, 100, price=190.10, benchmark_price=190.00)
    assert e.slippage_cost == pytest.approx(10.0)
    assert e.slippage_bps == pytest.approx(10.0 / 19_000 * 10_000)


def test_sell_slippage_is_positive_when_selling_low():
    e = Execution("AAPL", Side.SELL, 100, price=189.90, benchmark_price=190.00)
    assert e.slippage_cost == pytest.approx(10.0)


def test_price_improvement_is_negative_cost():
    e = Execution("AAPL", Side.BUY, 100, price=189.95, benchmark_price=190.00)
    assert e.slippage_cost == pytest.approx(-5.0)


def test_total_cost_includes_commission_and_fees():
    e = Execution("MSFT", "buy", 10, 400.0, 400.0, commission=1.5, fees=0.25)
    assert e.side is Side.BUY
    assert e.total_cost == pytest.approx(1.75)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"quantity": 0},
        {"price": -1},
        {"benchmark_price": 0},
        {"commission": -1},
    ],
)
def test_invalid_executions_raise(kwargs):
    base = dict(symbol="X", side=Side.BUY, quantity=1, price=1, benchmark_price=1)
    base.update(kwargs)
    with pytest.raises(ValueError):
        Execution(**base)


def make_tracker() -> ExecutionCostTracker:
    t = ExecutionCostTracker()
    t.record_many(
        [
            Execution("AAPL", Side.BUY, 100, 190.10, 190.00, commission=1.0, timestamp=ts(1), venue="A"),
            Execution("AAPL", Side.SELL, 50, 191.00, 191.20, commission=0.5, timestamp=ts(2), venue="B"),
            Execution("MSFT", Side.BUY, 10, 400.00, 399.00, fees=0.2, timestamp=ts(3), venue="A"),
        ]
    )
    return t


def test_summary_totals():
    s = make_tracker().summary()
    assert s.count == 3
    assert s.commission == pytest.approx(1.5)
    assert s.fees == pytest.approx(0.2)
    # slippage: 10 + 10 + 10
    assert s.slippage_cost == pytest.approx(30.0)
    assert s.total_cost == pytest.approx(31.7)
    assert s.total_cost_bps == pytest.approx(31.7 / s.benchmark_notional * 10_000)


def test_summary_by_symbol_and_venue():
    t = make_tracker()
    by_symbol = t.summary_by("symbol")
    assert set(by_symbol) == {"AAPL", "MSFT"}
    assert by_symbol["AAPL"].count == 2
    by_venue = t.summary_by("venue")
    assert by_venue["A"].count == 2


def test_filter():
    t = make_tracker()
    assert len(t.filter(symbol="AAPL")) == 2
    assert len(t.filter(side="sell")) == 1
    assert len(t.filter(start=ts(2), end=ts(3))) == 1


def test_empty_summary():
    s = ExecutionCostTracker().summary()
    assert s.count == 0
    assert s.total_cost_bps == 0.0


def test_csv_round_trip(tmp_path):
    t = make_tracker()
    path = tmp_path / "executions.csv"
    t.to_csv(path)
    loaded = ExecutionCostTracker.from_csv(path)
    assert loaded.executions == t.executions
    assert loaded.summary() == t.summary()
