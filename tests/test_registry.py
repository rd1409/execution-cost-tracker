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
    ],
)
def test_invalid_registry_is_rejected(mutate, match):
    with pytest.raises(RegistryError, match=match):
        registry.parse(broken(mutate))
