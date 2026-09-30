"""Data models for execution cost tracking."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        """+1 for buys, -1 for sells. Used so that paying more than the
        benchmark on a buy, or receiving less on a sell, is a positive cost."""
        return 1 if self is Side.BUY else -1


@dataclass(frozen=True)
class Execution:
    """A single fill (or aggregated order) and its costs.

    Attributes:
        symbol: Instrument identifier, e.g. "AAPL".
        side: Buy or sell.
        quantity: Units filled (must be positive).
        price: Average fill price.
        benchmark_price: Reference price used to measure slippage
            (e.g. arrival mid, decision price, VWAP).
        commission: Broker commission, in currency.
        fees: Exchange/regulatory/other fees, in currency.
        timestamp: When the execution happened (UTC by default).
        order_id: Optional identifier to link fills to an order.
        venue: Optional execution venue or broker.
    """

    symbol: str
    side: Side
    quantity: float
    price: float
    benchmark_price: float
    commission: float = 0.0
    fees: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    order_id: str | None = None
    venue: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.side, Side):
            object.__setattr__(self, "side", Side(str(self.side).lower()))
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.price <= 0 or self.benchmark_price <= 0:
            raise ValueError("price and benchmark_price must be positive")
        if self.commission < 0 or self.fees < 0:
            raise ValueError("commission and fees cannot be negative")

    @property
    def notional(self) -> float:
        """Traded value at the fill price."""
        return self.quantity * self.price

    @property
    def benchmark_notional(self) -> float:
        """Traded value at the benchmark price."""
        return self.quantity * self.benchmark_price

    @property
    def slippage_cost(self) -> float:
        """Currency cost of trading away from the benchmark.

        Positive means the fill was worse than the benchmark."""
        return self.side.sign * (self.price - self.benchmark_price) * self.quantity

    @property
    def explicit_cost(self) -> float:
        """Commission plus fees."""
        return self.commission + self.fees

    @property
    def total_cost(self) -> float:
        """Explicit costs plus slippage."""
        return self.explicit_cost + self.slippage_cost

    @property
    def total_cost_bps(self) -> float:
        """Total cost in basis points of benchmark notional."""
        return self.total_cost / self.benchmark_notional * 10_000

    @property
    def slippage_bps(self) -> float:
        """Slippage in basis points of benchmark notional."""
        return self.slippage_cost / self.benchmark_notional * 10_000


@dataclass(frozen=True)
class CostSummary:
    """Aggregated costs over a group of executions."""

    count: int
    quantity: float
    notional: float
    benchmark_notional: float
    commission: float
    fees: float
    slippage_cost: float

    @property
    def explicit_cost(self) -> float:
        return self.commission + self.fees

    @property
    def total_cost(self) -> float:
        return self.explicit_cost + self.slippage_cost

    @property
    def total_cost_bps(self) -> float:
        if self.benchmark_notional == 0:
            return 0.0
        return self.total_cost / self.benchmark_notional * 10_000

    @property
    def slippage_bps(self) -> float:
        if self.benchmark_notional == 0:
            return 0.0
        return self.slippage_cost / self.benchmark_notional * 10_000

    def as_dict(self) -> dict[str, float]:
        return {
            "count": self.count,
            "quantity": self.quantity,
            "notional": self.notional,
            "benchmark_notional": self.benchmark_notional,
            "commission": self.commission,
            "fees": self.fees,
            "explicit_cost": self.explicit_cost,
            "slippage_cost": self.slippage_cost,
            "total_cost": self.total_cost,
            "slippage_bps": self.slippage_bps,
            "total_cost_bps": self.total_cost_bps,
        }
