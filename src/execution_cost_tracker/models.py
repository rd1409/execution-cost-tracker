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


# --- FX tracking ---------------------------------------------------------
#
# A Pair trades a base token against a quote token (EURC/USDC: base EURC,
# quote USDC). Prices are always quoted as quote units per one base unit, so
# they compare directly with a traditional FX mid such as EUR/USD.


@dataclass(frozen=True)
class Token:
    """An ERC-20 token on a specific chain."""

    symbol: str
    address: str
    decimals: int

    def to_raw(self, amount: float) -> int:
        """Human units to integer base units."""
        return int(round(amount * 10**self.decimals))

    def from_raw(self, raw: int) -> float:
        """Integer base units to human units."""
        return raw / 10**self.decimals


@dataclass(frozen=True)
class Pair:
    """A tokenised FX pair on one chain.

    Attributes:
        base: The token being priced (e.g. EURC).
        quote: The token it is priced in (e.g. USDC).
        reference: The traditional FX pair it tracks, as "BASE/QUOTE"
            currency codes (e.g. "EUR/USD").
        chain: Chain name, e.g. "base".
        chain_id: EVM chain id, e.g. 8453.
        venue_params: Per-venue settings keyed by venue name, e.g.
            {"uniswap_v3": {"fee_tiers": [100, 500]}}.
    """

    base: Token
    quote: Token
    reference: str
    chain: str = "base"
    chain_id: int = 8453
    venue_params: dict = field(default_factory=dict, hash=False, compare=False)

    @property
    def name(self) -> str:
        return f"{self.base.symbol}/{self.quote.symbol}"

    def params_for(self, venue: str) -> dict:
        return dict(self.venue_params.get(venue, {}))


@dataclass(frozen=True)
class Quote:
    """One executable price from one venue for one size and direction.

    ``side`` is from the trader's point of view on the base token: "buy"
    means paying quote to receive base, "sell" means paying base to receive
    quote. Amounts are in human units.
    """

    venue: str
    pair: str
    chain: str
    side: Side
    base_amount: float
    quote_amount: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    gas_estimate: int | None = None
    meta: dict = field(default_factory=dict, hash=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.side, Side):
            object.__setattr__(self, "side", Side(str(self.side).lower()))
        if self.base_amount <= 0 or self.quote_amount <= 0:
            raise ValueError("base_amount and quote_amount must be positive")

    @property
    def price(self) -> float:
        """Quote units per one base unit."""
        return self.quote_amount / self.base_amount


@dataclass(frozen=True)
class ReferenceRate:
    """A traditional FX mid rate, e.g. EUR/USD = 1.0850."""

    pair: str
    mid: float
    source: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    as_of: str | None = None  # the source's own date/time label, if any

    def __post_init__(self) -> None:
        if self.mid <= 0:
            raise ValueError("mid must be positive")
