"""Token and pair registry loaded from ``config.yaml``.

The registry is the single source of truth for which chains, tokens and pairs
exist. The API validates user input against it, the front end builds its
dropdowns from it, and the scheduled run quotes every pair in it.

The file is looked up in this order: the ``FX_CONFIG`` env var, the repo root
(next to ``app.py``), then the current working directory.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .models import Pair, Token

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")  # base58 public key
_KINDS = ("evm", "solana")
_PROVIDERS = ("across", "relay", "layerzero")
_REFERENCE = re.compile(r"^[A-Z]{3}/[A-Z]{3}$")

# TradingView's published webhook sender addresses, used when config.yaml
# doesn't list them: https://www.tradingview.com/support/solutions/43000529348
DEFAULT_TRADINGVIEW_IPS = ("52.89.214.238", "34.212.75.30", "54.218.53.128", "52.32.178.7")


class RegistryError(ValueError):
    """config.yaml is missing or invalid."""


@dataclass(frozen=True)
class Chain:
    """A network a stablecoin can start or end on.

    ``gas_token`` is what a wallet needs to pay for transactions there.
    ``gas_price_product`` is the Coinbase product used to value it in USD
    (e.g. "ETH-USD"); ``gas_token_usd`` fixes the value instead (Tempo
    charges fees in USD stablecoins, so 1). ``bridge_ids`` holds the id each
    bridge API uses for the chain where it differs from ``chain_id``.
    """

    key: str
    name: str
    chain_id: int | None
    tokens: dict[str, Token] = field(hash=False, compare=False)
    kind: str = "evm"
    gas_token: str = "ETH"
    gas_price_product: str | None = None
    gas_token_usd: float | None = None
    gas_note: str = ""
    rpc_url: str | None = None
    bridge_ids: dict = field(default_factory=dict, hash=False, compare=False)

    @property
    def is_evm(self) -> bool:
        return self.kind == "evm"

    def bridge_id(self, provider: str):
        """The id ``provider`` uses for this chain (defaults: EVM chain id for
        Across and Relay, the chain key for LayerZero)."""
        if provider in self.bridge_ids:
            return self.bridge_ids[provider]
        if provider == "layerzero":
            return self.key
        return self.chain_id


@dataclass(frozen=True)
class RoutingConfig:
    """How stablecoins get to and from each venue (``routing`` in config.yaml)."""

    # Networks Coinbase accepts deposits on and sends withdrawals to, per token.
    coinbase_networks: dict = field(default_factory=dict, hash=False, compare=False)
    # Deposit networks to bridge to when the start chain isn't one of them.
    coinbase_hubs: tuple[str, ...] = ("base",)
    # Fixed Coinbase send fees {token: {network: amount}}; others are estimated from live gas.
    coinbase_withdrawal_fees: dict = field(default_factory=dict, hash=False, compare=False)
    # Which tokens and chains each bridge provider is asked about.
    bridges: dict = field(default_factory=dict, hash=False, compare=False)
    gas_units: dict = field(
        default_factory=lambda: {"transfer": 65_000, "approve": 50_000, "bridge": 150_000, "swap": 180_000},
        hash=False,
        compare=False,
    )
    solana_fee_lamports: int = 10_000
    # Placeholder wallets sent to bridge APIs for quotes only; nothing is ever sent from them.
    quote_wallets: dict = field(default_factory=dict, hash=False, compare=False)

    def coinbase_accepts(self, token: str, chain: str) -> bool:
        return chain in self.coinbase_networks.get(token, ())

    def bridge_covers(self, provider: str, token: str, from_chain: str, to_chain: str) -> bool:
        cfg = self.bridges.get(provider) or {}
        chains = cfg.get("chains", ())
        return token in cfg.get("tokens", ()) and from_chain in chains and to_chain in chains


@dataclass(frozen=True)
class Registry:
    chains: dict[str, Chain] = field(hash=False)
    pairs: tuple[Pair, ...]
    min_notional: float = 1.0
    max_notional: float = 1_000_000.0
    per_client_per_minute: int = 10
    global_per_minute: int = 60
    cache_seconds: float = 15.0
    cron_sizes: tuple[float, ...] = (1_000.0, 10_000.0, 100_000.0)
    max_distance_from_mid_pct: float = 2.0
    routing: RoutingConfig = field(default_factory=RoutingConfig, hash=False, compare=False)
    market_mid_max_age_seconds: float = 180.0
    market_mid_symbols: dict = field(default_factory=lambda: {"EURUSD": "EUR/USD"}, hash=False, compare=False)
    tradingview_ips: tuple[str, ...] = DEFAULT_TRADINGVIEW_IPS
    enforce_ip_allowlist: bool = True

    def reference_for_symbol(self, ticker: str) -> str | None:
        """The reference pair (e.g. "EUR/USD") a TradingView ticker prices, if
        configured. Matches "EURUSD", "FX:EURUSD" and "eurusd" alike."""
        key = str(ticker).split(":")[-1].replace("/", "").upper()
        return self.market_mid_symbols.get(key)

    def pair(self, name: str, chain: str) -> Pair | None:
        """The pair called ``name`` (e.g. "EURC/USDC") on ``chain``, if listed."""
        for p in self.pairs:
            if p.name == name and p.chain == chain:
                return p
        return None

    def pairs_on(self, chain: str) -> list[Pair]:
        return [p for p in self.pairs if p.chain == chain]

    def pair_names(self) -> list[str]:
        return sorted({p.name for p in self.pairs})

    def stablecoins(self) -> list[str]:
        """Every token symbol listed on any chain, in first-seen order."""
        return list(dict.fromkeys(sym for c in self.chains.values() for sym in c.tokens))

    def trade_pair(self, a: str, b: str, chain: str) -> Pair | None:
        """The listed pair on ``chain`` made of tokens ``a`` and ``b`` in either order."""
        for p in self.pairs:
            if p.chain == chain and {p.base.symbol, p.quote.symbol} == {a, b}:
                return p
        return None


def default_path() -> Path:
    env = os.environ.get("FX_CONFIG")
    if env:
        return Path(env)
    repo_root = Path(__file__).resolve().parents[2] / "config.yaml"
    if repo_root.exists():
        return repo_root
    return Path.cwd() / "config.yaml"


def load(path: str | Path | None = None) -> Registry:
    """Read and validate a registry file."""
    import yaml  # third-party; only needed here

    path = Path(path) if path is not None else default_path()
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise RegistryError(f"registry file not found: {path}") from None
    except yaml.YAMLError as e:
        raise RegistryError(f"{path} is not valid YAML: {e}") from None
    return parse(raw, source=str(path))


def parse(raw: object, source: str = "config") -> Registry:
    """Validate an already-parsed registry mapping."""

    def fail(msg: str) -> RegistryError:
        return RegistryError(f"{source}: {msg}")

    if not isinstance(raw, dict):
        raise fail("top level must be a mapping")

    chains: dict[str, Chain] = {}
    for key, c in (raw.get("chains") or {}).items():
        if not isinstance(c, dict):
            raise fail(f"chain {key!r} must be a mapping")
        kind = str(c.get("kind", "evm"))
        if kind not in _KINDS:
            raise fail(f"chain {key!r} kind must be one of {', '.join(_KINDS)}")
        chain_id = c.get("chain_id")
        if kind == "evm" or chain_id is not None:
            if not isinstance(chain_id, int) or isinstance(chain_id, bool) or chain_id <= 0:
                raise fail(f"chain {key!r} needs a positive integer chain_id")
        address_re = _ADDRESS if kind == "evm" else _SOLANA_ADDRESS
        tokens: dict[str, Token] = {}
        for sym, t in (c.get("tokens") or {}).items():
            if not isinstance(t, dict):
                raise fail(f"token {sym} on {key} must be a mapping")
            address = str(t.get("address", ""))
            if not address_re.match(address):
                raise fail(f"token {sym} on {key} has an invalid address {address!r}")
            decimals = t.get("decimals")
            if not isinstance(decimals, int) or not 0 <= decimals <= 36:
                raise fail(f"token {sym} on {key} needs integer decimals between 0 and 36")
            tokens[str(sym)] = Token(str(sym), address, decimals, str(t.get("label") or ""))
        usd = c.get("gas_token_usd")
        if usd is not None and (not isinstance(usd, (int, float)) or usd <= 0):
            raise fail(f"chain {key!r} gas_token_usd must be a positive number")
        product = c.get("gas_price_product")
        if product is not None and not re.match(r"^[A-Z0-9]+-[A-Z0-9]+$", str(product)):
            raise fail(f"chain {key!r} gas_price_product must look like 'ETH-USD', got {product!r}")
        bridge_ids = c.get("bridge_ids") or {}
        if not isinstance(bridge_ids, dict) or any(k not in _PROVIDERS for k in bridge_ids):
            raise fail(f"chain {key!r} bridge_ids may only set {', '.join(_PROVIDERS)}")
        chains[str(key)] = Chain(
            str(key),
            str(c.get("name", key)),
            chain_id,
            tokens,
            kind=kind,
            gas_token=str(c.get("gas_token", "ETH")),
            gas_price_product=str(product) if product else None,
            gas_token_usd=float(usd) if usd is not None else None,
            gas_note=str(c.get("gas_note") or ""),
            rpc_url=str(c["rpc_url"]) if c.get("rpc_url") else None,
            bridge_ids=dict(bridge_ids),
        )
    if not chains:
        raise fail("no chains defined")

    pairs: list[Pair] = []
    seen: set[tuple[str, str]] = set()
    for i, p in enumerate(raw.get("pairs") or []):
        if not isinstance(p, dict):
            raise fail(f"pair #{i + 1} must be a mapping")
        base, quote = str(p.get("base", "")), str(p.get("quote", ""))
        reference = str(p.get("reference", ""))
        if not base or not quote or base == quote:
            raise fail(f"pair #{i + 1} needs different base and quote tokens")
        if not _REFERENCE.match(reference):
            raise fail(f"pair {base}/{quote} needs a reference like 'EUR/USD', got {reference!r}")
        on = p.get("chains") or []
        if not on:
            raise fail(f"pair {base}/{quote} lists no chains")
        for chain_key in on:
            chain = chains.get(chain_key)
            if chain is None:
                raise fail(f"pair {base}/{quote} uses unknown chain {chain_key!r}")
            if not chain.is_evm:
                raise fail(f"pair {base}/{quote}: on-chain venues are EVM only, so it can't trade on {chain_key}")
            for sym in (base, quote):
                if sym not in chain.tokens:
                    raise fail(f"pair {base}/{quote} needs token {sym} on chain {chain_key}")
            if (f"{base}/{quote}", chain_key) in seen:
                raise fail(f"pair {base}/{quote} is listed twice on {chain_key}")
            seen.add((f"{base}/{quote}", chain_key))
            _check_venue_params(p.get("venue_params"), f"{base}/{quote}", fail)
            pairs.append(
                Pair(
                    base=chain.tokens[base],
                    quote=chain.tokens[quote],
                    reference=reference,
                    chain=chain_key,
                    chain_id=chain.chain_id,
                    venue_params=dict(p.get("venue_params") or {}),
                )
            )
    if not pairs:
        raise fail("no pairs defined")

    limits = raw.get("limits") or {}
    guard = raw.get("guardrails") or {}
    cron = raw.get("cron") or {}
    quality = raw.get("quality") or {}
    mm = raw.get("market_mid") or {}
    symbols = mm.get("symbols", {"EURUSD": "EUR/USD"}) or {}
    if not isinstance(symbols, dict):
        raise fail("market_mid.symbols must map TradingView tickers to pairs like 'EUR/USD'")
    norm_symbols = {}
    for ticker, ref in symbols.items():
        if not _REFERENCE.match(str(ref)):
            raise fail(f"market_mid.symbols.{ticker} must be a pair like 'EUR/USD', got {ref!r}")
        norm_symbols[str(ticker).replace("/", "").upper()] = str(ref)
    ips = mm.get("tradingview_ips", list(DEFAULT_TRADINGVIEW_IPS)) or []
    if not isinstance(ips, list) or not all(isinstance(ip, str) and ip.count(".") == 3 for ip in ips):
        raise fail("market_mid.tradingview_ips must be a list of IPv4 addresses")
    enforce = mm.get("enforce_ip_allowlist", True)
    if not isinstance(enforce, bool):
        raise fail("market_mid.enforce_ip_allowlist must be true or false")
    if enforce and not ips:
        raise fail("market_mid.enforce_ip_allowlist is true but tradingview_ips is empty")
    routing = _parse_routing(raw.get("routing"), chains, fail)
    try:
        reg = Registry(
            chains=chains,
            routing=routing,
            pairs=tuple(pairs),
            min_notional=float(limits.get("min_notional", 1)),
            max_notional=float(limits.get("max_notional", 1_000_000)),
            per_client_per_minute=int(guard.get("per_client_per_minute", 10)),
            global_per_minute=int(guard.get("global_per_minute", 60)),
            cache_seconds=float(guard.get("cache_seconds", 15)),
            cron_sizes=tuple(float(s) for s in cron.get("sizes", [1_000, 10_000, 100_000])),
            max_distance_from_mid_pct=float(quality.get("max_distance_from_mid_pct", 2)),
            market_mid_max_age_seconds=float(mm.get("max_age_seconds", 180)),
            market_mid_symbols=norm_symbols,
            tradingview_ips=tuple(ips),
            enforce_ip_allowlist=enforce,
        )
    except (TypeError, ValueError) as e:
        raise fail(f"limits/guardrails/cron/quality/market_mid values must be numbers: {e}") from None
    if not 0 < reg.min_notional <= reg.max_notional:
        raise fail("limits need 0 < min_notional <= max_notional")
    if reg.per_client_per_minute < 1 or reg.global_per_minute < 1 or reg.cache_seconds < 0:
        raise fail("guardrails must be positive (cache_seconds may be 0 to disable)")
    if not reg.cron_sizes or any(s <= 0 for s in reg.cron_sizes):
        raise fail("cron.sizes must be a non-empty list of positive numbers")
    if not 0 < reg.max_distance_from_mid_pct <= 100:
        raise fail("quality.max_distance_from_mid_pct must be above 0 and at most 100")
    if reg.market_mid_max_age_seconds <= 0:
        raise fail("market_mid.max_age_seconds must be positive")
    return reg


def _check_venue_params(vp: object, pair: str, fail) -> None:
    """Validate the per-venue settings that have known keys."""
    if vp is None:
        return
    if not isinstance(vp, dict):
        raise fail(f"pair {pair}: venue_params must be a mapping")
    cb = vp.get("coinbase")
    if cb is None:
        return
    if not isinstance(cb, dict):
        raise fail(f"pair {pair}: venue_params.coinbase must be a mapping")
    pid = cb.get("product_id")
    if not isinstance(pid, str) or not re.match(r"^[A-Z0-9]+-[A-Z0-9]+$", pid):
        raise fail(f"pair {pair}: coinbase.product_id must look like 'EURC-USDC', got {pid!r}")
    depth = cb.get("depth_limit", 500)
    if not isinstance(depth, int) or not 1 <= depth <= 10_000:
        raise fail(f"pair {pair}: coinbase.depth_limit must be a whole number from 1 to 10000")
    fee = cb.get("taker_fee_bps", 0)
    if not isinstance(fee, (int, float)) or not 0 <= fee <= 100:
        raise fail(f"pair {pair}: coinbase.taker_fee_bps must be between 0 and 100")


def _parse_routing(raw: object, chains: dict[str, Chain], fail) -> RoutingConfig:
    """Validate the ``routing`` section; every chain and token it names must exist."""
    if raw is None:
        return RoutingConfig()
    if not isinstance(raw, dict):
        raise fail("routing must be a mapping")
    symbols = {sym for c in chains.values() for sym in c.tokens}

    def chain_list(value, where: str) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise fail(f"{where} must be a list of chain names")
        unknown = [v for v in value if v not in chains]
        if unknown:
            raise fail(f"{where} names unknown chain(s) {', '.join(unknown)}")
        return tuple(value)

    def token_list(value, where: str) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise fail(f"{where} must be a list of token symbols")
        unknown = [v for v in value if v not in symbols]
        if unknown:
            raise fail(f"{where} names unknown token(s) {', '.join(unknown)}")
        return tuple(value)

    cb = raw.get("coinbase") or {}
    if not isinstance(cb, dict):
        raise fail("routing.coinbase must be a mapping")
    networks = {}
    for sym, on in (cb.get("networks") or {}).items():
        if sym not in symbols:
            raise fail(f"routing.coinbase.networks names unknown token {sym}")
        networks[sym] = chain_list(on, f"routing.coinbase.networks.{sym}")
        missing = [ch for ch in networks[sym] if sym not in chains[ch].tokens]
        if missing:
            raise fail(f"routing.coinbase.networks.{sym}: {sym} isn't listed on {', '.join(missing)}")
    hubs = chain_list(cb.get("bridge_hubs", ["base"]), "routing.coinbase.bridge_hubs")
    fees = cb.get("withdrawal_fees") or {}
    if not isinstance(fees, dict):
        raise fail("routing.coinbase.withdrawal_fees must map tokens to {network: fee}")
    for sym, per in fees.items():
        if sym not in symbols or not isinstance(per, dict):
            raise fail(f"routing.coinbase.withdrawal_fees.{sym} must be a mapping for a known token")
        for net, fee in per.items():
            if net not in chains or not isinstance(fee, (int, float)) or fee < 0:
                raise fail(f"routing.coinbase.withdrawal_fees.{sym}.{net} must be a non-negative number for a known chain")

    bridges = {}
    for name, b in (raw.get("bridges") or {}).items():
        if name not in _PROVIDERS:
            raise fail(f"routing.bridges.{name}: unknown provider; choose from {', '.join(_PROVIDERS)}")
        if not isinstance(b, dict):
            raise fail(f"routing.bridges.{name} must be a mapping")
        bridges[name] = {
            "tokens": token_list(b.get("tokens", []), f"routing.bridges.{name}.tokens"),
            "chains": chain_list(b.get("chains", []), f"routing.bridges.{name}.chains"),
        }

    units = dict(RoutingConfig().gas_units)
    for k, v in (raw.get("gas_units") or {}).items():
        if k not in units or not isinstance(v, int) or v <= 0:
            raise fail(f"routing.gas_units.{k} must be a positive whole number for one of {', '.join(units)}")
        units[k] = v
    lamports = raw.get("solana_fee_lamports", 10_000)
    if not isinstance(lamports, int) or lamports < 0:
        raise fail("routing.solana_fee_lamports must be a non-negative whole number")
    wallets = raw.get("quote_wallets") or {}
    if not isinstance(wallets, dict):
        raise fail("routing.quote_wallets must be a mapping")
    if wallets.get("evm") and not _ADDRESS.match(str(wallets["evm"])):
        raise fail("routing.quote_wallets.evm must be a 0x address")
    if wallets.get("solana") and not _SOLANA_ADDRESS.match(str(wallets["solana"])):
        raise fail("routing.quote_wallets.solana must be a base58 address")
    return RoutingConfig(
        coinbase_networks=networks,
        coinbase_hubs=hubs,
        coinbase_withdrawal_fees={k: dict(v) for k, v in fees.items()},
        bridges=bridges,
        gas_units=units,
        solana_fee_lamports=lamports,
        quote_wallets={k: str(v) for k, v in wallets.items()},
    )


@lru_cache(maxsize=1)
def get() -> Registry:
    """The registry from the default path, loaded once per process."""
    return load()
