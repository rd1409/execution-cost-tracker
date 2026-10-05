"""Route planner: price a stablecoin conversion end to end.

A request names a start stablecoin and chain, an end stablecoin and chain, and
an amount. For every venue that can trade the pair, the planner builds a
route of up to three steps and prices each one:

1. **To the venue.**
   * A DEX on the start chain needs nothing. A DEX elsewhere needs a bridge
     (Across, Relay or LayerZero; the cheapest quote wins).
   * Coinbase: a plain send if Coinbase accepts the token on the start chain
     (``routing.coinbase.networks``). Otherwise a bridge to one of
     ``routing.coinbase.bridge_hubs``, with your Coinbase deposit address as
     the recipient, so there's no separate deposit transaction.
2. **The trade.** The venue's own simulation: DEX quoters, or a walk of the
   Coinbase order book including the taker fee.
3. **To the destination.**
   * A DEX on the end chain needs nothing; otherwise a bridge.
   * Coinbase: a withdrawal (Coinbase deducts its network-fee estimate) if it
     sends the token on the end chain; otherwise a withdrawal to a hub and a
     bridge from there.

Each step records the chain you sign on and its gas token, so the page can
list the gas tokens a route needs. Routes are ranked by what arrives after
gas and bridge fees. Nothing here talks to the network directly: venues,
bridges and the gas oracle are passed in, so tests use fakes.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from . import metrics
from .bridges import Bridge, BridgeQuote
from .gas import GasCost, GasError, GasOracle, Memo
from .models import Pair, Token
from .registry import Registry
from .venues import Venue

COINBASE = "coinbase"  # the trade place for Coinbase (off-chain)


class RouteError(Exception):
    """One step of a route couldn't be priced; the route is skipped."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


@dataclass
class RouteRequest:
    from_token: str
    from_chain: str
    to_token: str
    to_chain: str
    amount: float
    venues: Sequence[str]


@dataclass
class Leg:
    """One step of a route. ``signer_chain`` is where your wallet signs a
    transaction (and so needs that chain's gas token); None for steps Coinbase
    performs. ``fee_usd`` is charged on top of the token amounts."""

    step: str  # "to_venue" | "trade" | "to_destination"
    kind: str  # "deposit" | "bridge" | "swap" | "withdraw"
    provider: str
    description: str
    from_place: str
    to_place: str
    token_in: str
    token_out: str
    amount_in: float
    amount_out: float
    signer_chain: str | None = None
    gas: GasCost | None = None
    gas_error: str | None = None
    fee_usd: float = 0.0
    eta_seconds: float | None = None
    alternatives: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "step": self.step,
            "kind": self.kind,
            "provider": self.provider,
            "description": self.description,
            "from": self.from_place,
            "to": self.to_place,
            "token_in": self.token_in,
            "token_out": self.token_out,
            "amount_in": self.amount_in,
            "amount_out": self.amount_out,
            "signer_chain": self.signer_chain,
            "gas": self.gas.as_dict() if self.gas else None,
            "gas_error": self.gas_error,
            "fee_usd": self.fee_usd,
            "eta_seconds": self.eta_seconds,
            "alternatives": self.alternatives,
            "meta": self.meta,
        }


@dataclass
class RoutePlan:
    request: RouteRequest
    pair: str
    selling_base: bool
    ref_mid: float
    routes: list[dict] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)


class RoutePlanner:
    def __init__(
        self,
        reg: Registry,
        venues: Mapping[str, Venue],
        bridges: Sequence[Bridge],
        gas: GasOracle,
        ref_mid: float,
        band_pct: float | None = None,
        venue_labels: Mapping[str, str] | None = None,
        max_workers: int = 8,
    ) -> None:
        if ref_mid <= 0:
            raise ValueError("ref_mid must be positive")
        self.reg = reg
        self.venues = dict(venues)
        self.bridges = list(bridges)
        self.gas = gas
        self.ref_mid = ref_mid
        self.band_pct = reg.max_distance_from_mid_pct if band_pct is None else band_pct
        self.labels = dict(venue_labels or {})
        self.max_workers = max_workers
        self._bridge_memo = Memo()
        self._io: ThreadPoolExecutor | None = None

    # --- helpers -------------------------------------------------------------

    def chain_name(self, place: str) -> str:
        if place == COINBASE:
            return "Coinbase"
        c = self.reg.chains.get(place)
        return c.name if c else place

    def token_on(self, symbol: str, place: str) -> str:
        c = self.reg.chains.get(place)
        t = c.tokens.get(symbol) if c else None
        return t.display if t else symbol

    def pairs_for(self, a: str, b: str) -> list[Pair]:
        return [p for p in self.reg.pairs if {p.base.symbol, p.quote.symbol} == {a, b}]

    def usd_per(self, symbol: str, pair: Pair) -> float:
        """USD value of one unit of ``symbol`` (the pair's quote currency is USD)."""
        return self.ref_mid if symbol == pair.base.symbol else 1.0

    def _gas(self, chain: str, units: int) -> tuple[GasCost | None, str | None]:
        try:
            return self.gas.cost(chain, units), None
        except (GasError, KeyError) as e:
            return None, str(e)

    def _gas_from_usd(self, chain: str, usd: float) -> GasCost:
        c = self.reg.chains[chain]
        try:
            native = usd / self.gas.gas_token_usd(chain)
        except GasError:
            native = None
        return GasCost(chain, c.gas_token, None, native, usd)

    # --- planning ---------------------------------------------------------------

    def candidates(self, req: RouteRequest) -> list[tuple[str, str, Pair]]:
        """(venue, trade place, pair) for every requested venue that can trade the pair."""
        out = []
        pairs = self.pairs_for(req.from_token, req.to_token)
        for name in req.venues:
            venue = self.venues.get(name)
            if venue is None:
                continue
            if name == COINBASE:
                pair = next((p for p in pairs if venue.supports(p)), None)
                if pair is not None:
                    out.append((name, COINBASE, pair))
                continue
            for p in pairs:
                if venue.supports(p):
                    out.append((name, p.chain, p))
        return out

    def plan(self, req: RouteRequest) -> RoutePlan:
        pairs = self.pairs_for(req.from_token, req.to_token)
        if not pairs:
            raise ValueError(f"no listed pair trades {req.from_token} for {req.to_token}")
        base = pairs[0].base.symbol
        plan = RoutePlan(req, pairs[0].name, req.from_token == base, self.ref_mid)
        cands = self.candidates(req)
        with ThreadPoolExecutor(max_workers=12) as io, ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            self._io = io
            futures = [(c, pool.submit(self.build, req, *c)) for c in cands]
            for (name, place, _pair), fut in futures:
                try:
                    route = fut.result()
                except RouteError as e:
                    plan.failures.append({"venue": name, "place": place, "stage": e.stage, "error": str(e)[:400]})
                    continue
                except Exception as e:  # a venue or bridge bug must never sink the request
                    plan.failures.append(
                        {"venue": name, "place": place, "stage": "unknown", "error": f"{type(e).__name__}: {e}"[:400]}
                    )
                    continue
                (plan.routes if route["within_band"] else plan.excluded).append(route)
        self._io = None
        plan.routes.sort(key=lambda r: -r["net_amount_out"])
        return plan

    def build(self, req: RouteRequest, venue_name: str, place: str, pair: Pair) -> dict:
        legs: list[Leg] = []
        amount = req.amount

        # 1. to the venue
        legs += self.inbound(req.from_token, req.from_chain, place, amount, pair)
        if legs:
            amount = legs[-1].amount_out

        # 2. the trade
        trade = self.trade(venue_name, place, pair, req.from_token, req.to_token, amount)
        legs.append(trade)
        amount = trade.amount_out

        # 3. to the destination
        legs += self.outbound(req.to_token, place, req.to_chain, amount, pair)
        return self.summarise(req, venue_name, place, pair, legs, trade)

    # --- step 1 ------------------------------------------------------------------

    def inbound(self, token: str, start: str, place: str, amount: float, pair: Pair) -> list[Leg]:
        if place != COINBASE:
            if start == place:
                return []
            return [self.bridge_leg("to_venue", token, start, place, amount, pair)]

        routing = self.reg.routing
        if routing.coinbase_accepts(token, start):
            gas, err = self._gas(start, routing.gas_units["transfer"])
            return [
                Leg(
                    step="to_venue",
                    kind="deposit",
                    provider=COINBASE,
                    description=f"Send {self.token_on(token, start)} on {self.chain_name(start)} to your Coinbase deposit address",
                    from_place=start,
                    to_place=COINBASE,
                    token_in=token,
                    token_out=token,
                    amount_in=amount,
                    amount_out=amount,
                    signer_chain=start,
                    gas=gas,
                    gas_error=err,
                    meta={"network": start},
                )
            ]
        hubs = [h for h in routing.coinbase_hubs if h != start and routing.coinbase_accepts(token, h)]
        if not hubs:
            raise RouteError(
                "to_venue",
                f"Coinbase doesn't take {token} on {self.chain_name(start)}, and no bridge hub that it accepts is configured",
            )
        leg = self._best_of_hubs(
            [lambda h=h: self.bridge_leg("to_venue", token, start, h, amount, pair) for h in hubs], "to_venue", token, pair
        )
        leg.description += ", straight to your Coinbase deposit address"
        leg.to_place = COINBASE
        leg.meta["deposit_network"] = leg.meta.get("bridge_to")
        return [leg]

    # --- step 2 ------------------------------------------------------------------

    def trade(self, venue_name: str, place: str, pair: Pair, sym_in: str, sym_out: str, amount: float) -> Leg:
        venue = self.venues[venue_name]
        t_in: Token = pair.base if pair.base.symbol == sym_in else pair.quote
        t_out: Token = pair.quote if t_in is pair.base else pair.base
        try:
            result = venue.simulate(pair, t_in, t_out, t_in.to_raw(amount))
        except Exception as e:
            raise RouteError("trade", f"{self.labels.get(venue_name, venue_name)}: {e}") from e
        out = t_out.from_raw(result.amount_out)
        if out <= 0:
            raise RouteError("trade", f"{self.labels.get(venue_name, venue_name)}: zero output")
        selling = t_in is pair.base
        price = out / amount if selling else amount / out
        label = self.labels.get(venue_name, venue_name)
        if place == COINBASE:
            desc = f"{'Sell' if selling else 'Buy'} {pair.base.symbol} on Coinbase, sweeping the order book as a taker"
            gas, err, signer = None, None, None
        else:
            units = (result.gas_estimate or self.reg.routing.gas_units["swap"]) + self.reg.routing.gas_units["approve"]
            gas, err = self._gas(place, units)
            signer = place
            desc = f"Swap {self.token_on(sym_in, place)} for {self.token_on(sym_out, place)} on {label} ({self.chain_name(place)})"
        return Leg(
            step="trade",
            kind="swap",
            provider=venue_name,
            description=desc,
            from_place=place,
            to_place=place,
            token_in=sym_in,
            token_out=sym_out,
            amount_in=amount,
            amount_out=out,
            signer_chain=signer,
            gas=gas,
            gas_error=err,
            meta={"price": price, "side": "sell" if selling else "buy", **(result.meta or {})},
        )

    # --- step 3 ------------------------------------------------------------------

    def outbound(self, token: str, place: str, end: str, amount: float, pair: Pair) -> list[Leg]:
        if place != COINBASE:
            if end == place:
                return []
            return [self.bridge_leg("to_destination", token, place, end, amount, pair)]

        routing = self.reg.routing
        if routing.coinbase_accepts(token, end):
            return [self.withdraw_leg(token, end, amount, pair, final=True)]
        hubs = [h for h in routing.coinbase_hubs if h != end and routing.coinbase_accepts(token, h)]
        if not hubs:
            raise RouteError(
                "to_destination",
                f"Coinbase doesn't send {token} on {self.chain_name(end)}, and no bridge hub that it supports is configured",
            )

        def via(hub: str):
            def run() -> list[Leg]:
                w = self.withdraw_leg(token, hub, amount, pair, final=False)
                b = self.bridge_leg("to_destination", token, hub, end, w.amount_out, pair)
                return [w, b]

            return run

        return self._best_of_hubs([via(h) for h in hubs], "to_destination", token, pair, multi=True)

    def withdraw_leg(self, token: str, network: str, amount: float, pair: Pair, final: bool) -> Leg:
        fixed = (self.reg.routing.coinbase_withdrawal_fees.get(token) or {}).get(network)
        note = None
        if fixed is not None:
            fee, basis = float(fixed), "configured"
        else:
            gas, err = self._gas(network, self.reg.routing.gas_units["transfer"])
            if gas is None:
                fee, basis, note = 0.0, "unknown", f"Coinbase's send fee couldn't be estimated: {err}"
            else:
                fee, basis = gas.usd / self.usd_per(token, pair), "network estimate"
        if fee >= amount:
            raise RouteError("to_destination", f"Coinbase's send fee on {self.chain_name(network)} is more than the amount")
        where = "your destination wallet" if final else "your wallet"
        return Leg(
            step="to_destination",
            kind="withdraw",
            provider=COINBASE,
            description=f"Coinbase sends {self.token_on(token, network)} on {self.chain_name(network)} to {where}",
            from_place=COINBASE,
            to_place=network,
            token_in=token,
            token_out=token,
            amount_in=amount,
            amount_out=amount - fee,
            gas_error=note,
            meta={"network": network, "send_fee": fee, "send_fee_basis": basis},
        )

    # --- bridging ---------------------------------------------------------------

    def bridge_leg(self, step: str, token: str, a: str, b: str, amount: float, pair: Pair) -> Leg:
        try:
            chosen, gas, gas_err, alternatives = self.best_bridge(token, a, b, amount, pair)
        except RouteError as e:
            raise RouteError(step, str(e)) from e
        bridge = next(x for x in self.bridges if x.name == chosen.provider)
        return Leg(
            step=step,
            kind="bridge",
            provider=chosen.provider,
            description=(
                f"Bridge {self.token_on(token, a)} from {self.chain_name(a)} to {self.chain_name(b)}"
                f" ({self.token_on(token, b)}) via {bridge.label}"
            ),
            from_place=a,
            to_place=b,
            token_in=token,
            token_out=token,
            amount_in=amount,
            amount_out=chosen.amount_out,
            signer_chain=a,
            gas=gas,
            gas_error=gas_err,
            fee_usd=chosen.fee_usd,
            eta_seconds=chosen.eta_seconds,
            alternatives=alternatives,
            meta={"route": chosen.route, "bridge_to": b, **chosen.meta},
        )

    def best_bridge(self, token: str, a: str, b: str, amount: float, pair: Pair):
        """Ask every configured bridge, keep the one that leaves the most after
        gas and fees. Memoised per (token, from, to, amount) within a request."""
        return self._bridge_memo.get((token, a, b, round(amount, 6)), lambda: self._best_bridge(token, a, b, amount, pair))

    def _best_bridge(self, token: str, a: str, b: str, amount: float, pair: Pair):
        providers = [br for br in self.bridges if br.covers(self.reg, token, a, b)]
        where = f"{token} from {self.chain_name(a)} to {self.chain_name(b)}"
        if not providers:
            names = ", ".join(br.label for br in self.bridges) or "no bridges"
            raise RouteError(
                "bridge", f"no configured bridge moves {where} ({names}: not set up for this; see routing.bridges in config.yaml)"
            )

        def ask(br: Bridge):
            return br.quote(self.reg, token, a, b, amount)

        if self._io is not None:
            futures = [(br, self._io.submit(ask, br)) for br in providers]
            results = []
            for br, f in futures:
                try:
                    results.append((br, f.result(), None))
                except Exception as e:
                    results.append((br, None, e))
        else:
            results = []
            for br in providers:
                try:
                    results.append((br, ask(br), None))
                except Exception as e:
                    results.append((br, None, e))

        scored, errors = [], []
        per_unit = self.usd_per(token, pair)
        for br, q, err in results:
            if q is None:
                msg = str(err)
                errors.append((br.name, msg if msg.startswith(br.label.split()[0]) else f"{br.label}: {msg}"))
                continue
            gas, gas_err = self._bridge_gas(q, a)
            extra_usd = q.fee_usd + (gas.usd if gas else 0.0)
            scored.append((q.amount_out - extra_usd / per_unit, q, gas, gas_err))
        if not scored:
            raise RouteError("bridge", f"no bridge quoted {where}: " + "; ".join(e for _n, e in errors))
        scored.sort(key=lambda s: -s[0])
        _net, chosen, gas, gas_err = scored[0]
        alternatives = [
            {
                "provider": q.provider,
                "amount_out": q.amount_out,
                "fee_usd": q.fee_usd,
                "gas_usd": g.usd if g else None,
                "eta_seconds": q.eta_seconds,
                "net_amount_out": net,
                "chosen": q is chosen,
            }
            for net, q, g, _e in scored
        ] + [{"provider": name, "error": e} for name, e in errors]
        return chosen, gas, gas_err, alternatives

    def _bridge_gas(self, q: BridgeQuote, chain: str) -> tuple[GasCost | None, str | None]:
        if q.origin_gas_usd is not None:
            return self._gas_from_usd(chain, q.origin_gas_usd), None
        return self._gas(chain, q.gas_units or self.reg.routing.gas_units["bridge"])

    def _best_of_hubs(self, attempts, stage: str, token: str, pair: Pair, multi: bool = False):
        """Run each hub option and keep the one that delivers the most after gas."""
        best, best_net, errors = None, None, []
        for run in attempts:
            try:
                got = run()
            except RouteError as e:
                errors.append(str(e))
                continue
            legs = got if multi else [got]
            usd = sum((leg.gas.usd if leg.gas else 0.0) + leg.fee_usd for leg in legs)
            net = legs[-1].amount_out - usd / self.usd_per(token, pair)
            if best_net is None or net > best_net:
                best, best_net = got, net
        if best is None:
            raise RouteError(stage, "; ".join(errors) or f"no way to move {token} via a Coinbase network")
        return best

    # --- summary -----------------------------------------------------------------

    def summarise(self, req: RouteRequest, venue: str, place: str, pair: Pair, legs: list[Leg], trade: Leg) -> dict:
        amount_out = legs[-1].amount_out
        gas_usd = sum(leg.gas.usd for leg in legs if leg.gas)
        fees_usd = sum(leg.fee_usd for leg in legs)
        missing_gas = [leg.description for leg in legs if leg.signer_chain and leg.gas is None]
        net_out = amount_out - (gas_usd + fees_usd) / self.usd_per(req.to_token, pair)
        selling = req.from_token == pair.base.symbol

        def rate(out: float) -> float | None:
            if out <= 0:
                return None
            return out / req.amount if selling else req.amount / out

        side = "sell" if selling else "buy"
        trade_price = trade.meta["price"]
        gas_tokens: dict[str, dict] = {}
        for leg in legs:
            if not leg.signer_chain:
                continue
            c = self.reg.chains[leg.signer_chain]
            entry = gas_tokens.setdefault(
                c.key, {"chain": c.key, "chain_name": c.name, "gas_token": c.gas_token, "note": c.gas_note, "for": []}
            )
            entry["for"].append(leg.description)
        etas = [leg.eta_seconds for leg in legs if leg.eta_seconds is not None]
        all_in = rate(net_out)

        # Where the cost goes, in bps of what the full amount would buy at the
        # reference mid: the trade itself, bridge/send fees taken from the
        # tokens, and gas plus fees paid on top. The three add up to the total.
        mid = self.ref_mid
        to_out = (lambda x: x * mid) if selling else (lambda x: x / mid)
        ideal = to_out(req.amount)
        parts = {
            "trade": to_out(trade.amount_in) - trade.amount_out,
            "transfers": to_out(req.amount - trade.amount_in) + (trade.amount_out - amount_out),
            "gas": (gas_usd + fees_usd) / self.usd_per(req.to_token, pair),
        }
        breakdown = {k: v / ideal * 10_000 for k, v in parts.items()}
        breakdown["total"] = sum(breakdown.values())
        return {
            "venue": venue,
            "venue_label": self.labels.get(venue, venue),
            "place": place,
            "place_name": self.chain_name(place),
            "pair": pair.name,
            "side": side,
            "token_in": req.from_token,
            "token_out": req.to_token,
            "amount_in": req.amount,
            "amount_out": amount_out,
            "net_amount_out": net_out,
            "gas_usd": gas_usd,
            "fees_usd": fees_usd,
            "gas_incomplete": missing_gas,
            "trade_price": trade_price,
            "effective_rate": rate(amount_out),
            "all_in_rate": all_in,
            "all_in_cost_bps": metrics.deviation_bps(all_in, self.ref_mid, side) if all_in else None,
            "cost_breakdown_bps": breakdown,
            "within_band": metrics.within_band(trade_price, self.ref_mid, self.band_pct),
            "offset_pct": metrics.offset_pct(trade_price, self.ref_mid),
            "to_venue": [leg.as_dict() for leg in legs if leg.step == "to_venue"],
            "trade": trade.as_dict(),
            "to_destination": [leg.as_dict() for leg in legs if leg.step == "to_destination"],
            "legs": [leg.as_dict() for leg in legs],
            "gas_tokens": list(gas_tokens.values()),
            "eta_seconds": sum(etas) if etas else None,
        }
