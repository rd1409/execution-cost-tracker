"""The config.yaml registry: the shipped file loads, and bad files fail clearly."""

import copy

import pytest

from execution_cost_tracker import registry
from execution_cost_tracker.registry import RegistryError

VALID = {
    "chains": {
        "base": {
            "name": "Base",
            "chain_id": 8453,
            "tokens": {
                "USDC": {"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
                "EURC": {"address": "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42", "decimals": 6},
            },
        }
    },
    "pairs": [{"base": "EURC", "quote": "USDC", "reference": "EUR/USD", "chains": ["base"]}],
    "limits": {"min_notional": 10, "max_notional": 50000},
    "guardrails": {"per_client_per_minute": 3, "global_per_minute": 9, "cache_seconds": 5},
    "cron": {"sizes": [100, 1000]},
}


def test_shipped_config_loads():
    reg = registry.load()
    pair = reg.pair("EURC/USDC", "base")
    assert pair is not None and pair.chain_id == 8453 and pair.reference == "EUR/USD"
    assert pair.base.decimals == 6 and pair.quote.symbol == "USDC"
    assert reg.max_notional >= reg.min_notional > 0
    assert reg.cron_sizes and reg.per_client_per_minute >= 1


def test_parse_reads_all_sections():
    reg = registry.parse(VALID)
    assert reg.min_notional == 10 and reg.max_notional == 50000
    assert (reg.per_client_per_minute, reg.global_per_minute, reg.cache_seconds) == (3, 9, 5)
    assert reg.cron_sizes == (100.0, 1000.0)
    assert reg.max_distance_from_mid_pct == 2.0  # default when the section is missing
    assert registry.parse({**VALID, "quality": {"max_distance_from_mid_pct": 0.5}}).max_distance_from_mid_pct == 0.5
    assert reg.pair_names() == ["EURC/USDC"] and reg.pair("EURC/USDC", "arbitrum") is None
    assert [p.name for p in reg.pairs_on("base")] == ["EURC/USDC"]


def test_load_from_file(tmp_path, monkeypatch):
    path = tmp_path / "custom.yaml"
    path.write_text("chains:\n  base:\n    chain_id: 8453\n    tokens:\n"
                    "      A: {address: '0x" + "1" * 40 + "', decimals: 6}\n"
                    "      B: {address: '0x" + "2" * 40 + "', decimals: 18}\n"
                    "pairs:\n  - {base: A, quote: B, reference: EUR/USD, chains: [base]}\n")
    monkeypatch.setenv("FX_CONFIG", str(path))
    reg = registry.load()
    assert reg.pair("A/B", "base").quote.decimals == 18


def test_missing_and_malformed_files(tmp_path):
    with pytest.raises(RegistryError, match="not found"):
        registry.load(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("chains: [unclosed")
    with pytest.raises(RegistryError, match="not valid YAML"):
        registry.load(bad)


def broken(mutate):
    raw = copy.deepcopy(VALID)
    mutate(raw)
    return raw


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda r: r.update(chains={}), "no chains"),
        (lambda r: r["chains"]["base"].update(chain_id="8453"), "chain_id"),
        (lambda r: r["chains"]["base"]["tokens"]["USDC"].update(address="0x123"), "invalid address"),
        (lambda r: r["chains"]["base"]["tokens"]["USDC"].update(decimals=99), "decimals"),
        (lambda r: r["pairs"][0].update(quote="EURC"), "different base and quote"),
        (lambda r: r["pairs"][0].update(reference="eurusd"), "reference"),
        (lambda r: r["pairs"][0].update(chains=["arbitrum"]), "unknown chain"),
        (lambda r: r["pairs"][0].update(base="GBPC"), "needs token GBPC"),
        (lambda r: r["pairs"].append(dict(r["pairs"][0])), "listed twice"),
        (lambda r: r.update(pairs=[]), "no pairs"),
        (lambda r: r["limits"].update(min_notional=0), "min_notional"),
        (lambda r: r["limits"].update(max_notional=5), "min_notional"),
        (lambda r: r["guardrails"].update(per_client_per_minute=0), "guardrails"),
        (lambda r: r["cron"].update(sizes=[]), "cron.sizes"),
        (lambda r: r["limits"].update(max_notional="lots"), "numbers"),
        (lambda r: r.update(quality={"max_distance_from_mid_pct": 0}), "max_distance_from_mid_pct"),
        (lambda r: r.update(quality={"max_distance_from_mid_pct": 150}), "max_distance_from_mid_pct"),
        (lambda r: r.update(quality={"max_distance_from_mid_pct": "two"}), "numbers"),
    ],
)
def test_invalid_registry_is_rejected(mutate, match):
    with pytest.raises(RegistryError, match=match):
        registry.parse(broken(mutate))


def test_market_mid_settings():
    reg = registry.parse(VALID)
    assert reg.market_mid_symbols == {"EURUSD": "EUR/USD"} and reg.enforce_ip_allowlist
    assert reg.tradingview_ips == registry.DEFAULT_TRADINGVIEW_IPS and reg.market_mid_max_age_seconds == 180
    assert reg.reference_for_symbol("FX:EURUSD") == "EUR/USD" and reg.reference_for_symbol("eur/usd") == "EUR/USD"
    assert reg.reference_for_symbol("GBPUSD") is None
    custom = registry.parse({**VALID, "market_mid": {"symbols": {"eurusd": "EUR/USD", "GBPUSD": "GBP/USD"},
                                                    "max_age_seconds": 60, "enforce_ip_allowlist": False}})
    assert custom.reference_for_symbol("GBPUSD") == "GBP/USD" and custom.market_mid_max_age_seconds == 60


@pytest.mark.parametrize(
    "mm,match",
    [
        ({"symbols": {"EURUSD": "eurusd"}}, "EUR/USD"),
        ({"symbols": ["EURUSD"]}, "symbols"),
        ({"tradingview_ips": "1.2.3.4"}, "IPv4"),
        ({"tradingview_ips": []}, "empty"),
        ({"enforce_ip_allowlist": "yes"}, "true or false"),
        ({"max_age_seconds": 0}, "max_age_seconds"),
    ],
)
def test_invalid_market_mid_settings(mm, match):
    with pytest.raises(RegistryError, match=match):
        registry.parse({**VALID, "market_mid": mm})


# --- chains beyond EVM, and routing -------------------------------------------------


SOL = {
    "name": "Solana",
    "kind": "solana",
    "gas_token": "SOL",
    "gas_price_product": "SOL-USD",
    "bridge_ids": {"relay": 792703809},
    "tokens": {"USDC": {"address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "decimals": 6}},
}

ROUTING = {
    "coinbase": {"networks": {"USDC": ["base", "solana"], "EURC": ["base"]}, "bridge_hubs": ["base"],
                 "withdrawal_fees": {"USDC": {"base": 0}}},
    "bridges": {"across": {"tokens": ["USDC"], "chains": ["base", "solana"]}},
    "gas_units": {"swap": 200000},
    "solana_fee_lamports": 7000,
    "quote_wallets": {"evm": "0x" + "a" * 40, "solana": "So11111111111111111111111111111111111111112"},
}


def with_solana(**routing):
    raw = copy.deepcopy(VALID)
    raw["chains"]["solana"] = copy.deepcopy(SOL)
    raw["routing"] = {**copy.deepcopy(ROUTING), **routing}
    return raw


def test_shipped_config_has_six_chains_and_routing():
    reg = registry.load()
    assert list(reg.chains) == ["ethereum", "solana", "base", "arbitrum", "polygon", "tempo"]
    assert reg.chains["solana"].chain_id is None and reg.chains["solana"].bridge_id("across") == 34268394551451
    assert reg.chains["tempo"].tokens["EURC"].display == "EURC.e" and reg.chains["tempo"].gas_token_usd == 1
    assert "EURC" not in reg.chains["arbitrum"].tokens and "EURC" not in reg.chains["polygon"].tokens
    assert reg.chains["polygon"].bridge_id("layerzero") == "polygon" and reg.chains["base"].bridge_id("relay") == 8453
    assert reg.trade_pair("USDC", "EURC", "ethereum").name == "EURC/USDC"
    assert reg.stablecoins() == ["USDC", "EURC"]


def test_parse_routing_section():
    reg = registry.parse(with_solana())
    r = reg.routing
    assert r.coinbase_accepts("USDC", "solana") and not r.coinbase_accepts("EURC", "solana")
    assert r.coinbase_withdrawal_fees == {"USDC": {"base": 0}} and r.coinbase_hubs == ("base",)
    assert r.bridge_covers("across", "USDC", "base", "solana") and not r.bridge_covers("relay", "USDC", "base", "solana")
    assert r.gas_units["swap"] == 200000 and r.gas_units["transfer"] == 65000 and r.solana_fee_lamports == 7000
    assert reg.chains["solana"].tokens["USDC"].address.startswith("EPjF")


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda r: r["chains"]["solana"].update(kind="cosmos"), "kind"),
        (lambda r: r["chains"]["solana"]["tokens"]["USDC"].update(address="0x123"), "invalid address"),
        (lambda r: r["chains"]["base"].update(gas_token_usd=-1), "gas_token_usd"),
        (lambda r: r["chains"]["base"].update(gas_price_product="eth"), "gas_price_product"),
        (lambda r: r["chains"]["base"].update(bridge_ids={"wormhole": 1}), "bridge_ids"),
        (lambda r: r["pairs"][0].update(chains=["solana"]), "EVM only"),
        (lambda r: r["routing"]["coinbase"]["networks"].update(EURC=["solana"]), "EURC isn't listed on solana"),
        (lambda r: r["routing"]["coinbase"]["networks"].update(USDC=["mars"]), "unknown chain"),
        (lambda r: r["routing"]["coinbase"].update(bridge_hubs="base"), "list of chain names"),
        (lambda r: r["routing"]["coinbase"].update(withdrawal_fees={"USDC": {"base": -1}}), "non-negative"),
        (lambda r: r["routing"]["bridges"].update(wormhole={}), "unknown provider"),
        (lambda r: r["routing"]["bridges"]["across"].update(tokens=["DAI"]), "unknown token"),
        (lambda r: r["routing"].update(gas_units={"swap": 0}), "gas_units.swap"),
        (lambda r: r["routing"].update(gas_units={"teleport": 5}), "gas_units.teleport"),
        (lambda r: r["routing"].update(solana_fee_lamports=-5), "solana_fee_lamports"),
        (lambda r: r["routing"].update(quote_wallets={"evm": "nope"}), "quote_wallets.evm"),
    ],
)
def test_invalid_routing_is_rejected(mutate, match):
    raw = with_solana()
    mutate(raw)
    with pytest.raises(RegistryError, match=match):
        registry.parse(raw)
