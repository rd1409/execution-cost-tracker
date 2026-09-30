"""The ExecutionCostTracker: records executions and reports their costs."""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .models import CostSummary, Execution, Side

_CSV_FIELDS = [
    "timestamp",
    "symbol",
    "side",
    "quantity",
    "price",
    "benchmark_price",
    "commission",
    "fees",
    "order_id",
    "venue",
]


class ExecutionCostTracker:
    """Collects executions and summarises their explicit and implicit costs.

    Example:
        >>> tracker = ExecutionCostTracker()
        >>> tracker.record(Execution("AAPL", Side.BUY, 100, 190.10, 190.00, commission=1.0))
        >>> round(tracker.summary().total_cost, 2)
        11.0
    """

    def __init__(self, executions: Iterable[Execution] | None = None) -> None:
        self._executions: list[Execution] = list(executions or [])

    # --- recording -------------------------------------------------------

    def record(self, execution: Execution) -> None:
        """Add one execution."""
        self._executions.append(execution)

    def record_many(self, executions: Iterable[Execution]) -> None:
        """Add several executions."""
        self._executions.extend(executions)

    def __len__(self) -> int:
        return len(self._executions)

    def __iter__(self) -> Iterator[Execution]:
        return iter(self._executions)

    @property
    def executions(self) -> list[Execution]:
        return list(self._executions)

    # --- filtering -------------------------------------------------------

    def filter(
        self,
        *,
        symbol: str | None = None,
        side: Side | str | None = None,
        venue: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> "ExecutionCostTracker":
        """Return a new tracker containing only matching executions.

        ``start`` is inclusive and ``end`` is exclusive."""
        if side is not None and not isinstance(side, Side):
            side = Side(str(side).lower())

        def keep(e: Execution) -> bool:
            return (
                (symbol is None or e.symbol == symbol)
                and (side is None or e.side is side)
                and (venue is None or e.venue == venue)
                and (start is None or e.timestamp >= start)
                and (end is None or e.timestamp < end)
            )

        return ExecutionCostTracker(e for e in self._executions if keep(e))

    # --- reporting -------------------------------------------------------

    def summary(self) -> CostSummary:
        """Aggregate costs over all recorded executions."""
        return _summarise(self._executions)

    def summary_by(self, key: str | Callable[[Execution], object]) -> dict[object, CostSummary]:
        """Aggregate costs grouped by an attribute name (e.g. ``"symbol"``,
        ``"venue"``, ``"side"``) or by a function of the execution."""
        key_fn = key if callable(key) else (lambda e: getattr(e, key))
        groups: dict[object, list[Execution]] = defaultdict(list)
        for e in self._executions:
            groups[key_fn(e)].append(e)
        return {k: _summarise(v) for k, v in groups.items()}

    # --- persistence -----------------------------------------------------

    def to_csv(self, path: str | Path) -> None:
        """Write all executions to a CSV file."""
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
            writer.writeheader()
            for e in self._executions:
                writer.writerow(
                    {
                        "timestamp": e.timestamp.isoformat(),
                        "symbol": e.symbol,
                        "side": e.side.value,
                        "quantity": e.quantity,
                        "price": e.price,
                        "benchmark_price": e.benchmark_price,
                        "commission": e.commission,
                        "fees": e.fees,
                        "order_id": e.order_id or "",
                        "venue": e.venue or "",
                    }
                )

    @classmethod
    def from_csv(cls, path: str | Path) -> "ExecutionCostTracker":
        """Load executions from a CSV written by :meth:`to_csv`."""
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        return cls(
            Execution(
                symbol=r["symbol"],
                side=Side(r["side"]),
                quantity=float(r["quantity"]),
                price=float(r["price"]),
                benchmark_price=float(r["benchmark_price"]),
                commission=float(r.get("commission") or 0),
                fees=float(r.get("fees") or 0),
                timestamp=datetime.fromisoformat(r["timestamp"]),
                order_id=r.get("order_id") or None,
                venue=r.get("venue") or None,
            )
            for r in rows
        )


def _summarise(executions: Iterable[Execution]) -> CostSummary:
    count = 0
    quantity = notional = benchmark_notional = 0.0
    commission = fees = slippage = 0.0
    for e in executions:
        count += 1
        quantity += e.quantity
        notional += e.notional
        benchmark_notional += e.benchmark_notional
        commission += e.commission
        fees += e.fees
        slippage += e.slippage_cost
    return CostSummary(
        count=count,
        quantity=quantity,
        notional=notional,
        benchmark_notional=benchmark_notional,
        commission=commission,
        fees=fees,
        slippage_cost=slippage,
    )
