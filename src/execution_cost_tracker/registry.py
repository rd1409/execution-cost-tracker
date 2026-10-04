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
_REFERENCE = re.compile(r"^[A-Z]{3}/[A-Z]{3}$")

# TradingView's published webhook sender addresses, used when config.yaml
# doesn't list them: https://www.tradingview.com/support/solutions/43000529348
DEFAULT_TRADINGVIEW_IPS = ("52.89.214.238", "34.212.75.30", "54.218.53.128", "52.32.178.7")


class RegistryError(ValueError):
    """config.yaml is missing or invalid."""


@dataclass(frozen=True)
class Chain:
    key: str
    name: str
    chain_id: int
    tokens: dict[str, Token] = field(hash=False, compare=False)


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
        chain_id = c.get("chain_id")
        if not isinstance(chain_id, int) or chain_id <= 0:
            raise fail(f"chain {key!r} needs a positive integer chain_id")
        tokens: dict[str, Token] = {}
        for sym, t in (c.get("tokens") or {}).items():
            if not isinstance(t, dict):
                raise fail(f"token {sym} on {key} must be a mapping")
            address = str(t.get("address", ""))
            if not _ADDRESS.match(address):
                raise fail(f"token {sym} on {key} has an invalid address {address!r}")
            decimals = t.get("decimals")
            if not isinstance(decimals, int) or not 0 <= decimals <= 36:
                raise fail(f"token {sym} on {key} needs integer decimals between 0 and 36")
            tokens[str(sym)] = Token(str(sym), address, decimals)
        chains[str(key)] = Chain(str(key), str(c.get("name", key)), chain_id, tokens)
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
            for sym in (base, quote):
                if sym not in chain.tokens:
                    raise fail(f"pair {base}/{quote} needs token {sym} on chain {chain_key}")
            if (f"{base}/{quote}", chain_key) in seen:
                raise fail(f"pair {base}/{quote} is listed twice on {chain_key}")
            seen.add((f"{base}/{quote}", chain_key))
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
    try:
        reg = Registry(
            chains=chains,
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


@lru_cache(maxsize=1)
def get() -> Registry:
    """The registry from the default path, loaded once per process."""
    return load()
