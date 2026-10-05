"""Bridge clients and the gas oracle, offline (fake HTTP)."""

import urllib.parse

import pytest

from execution_cost_tracker import httpjson, registry
from execution_cost_tracker.bridges import Across, BridgeError, LayerZero, Relay
from execution_cost_tracker.gas import GasError, GasOracle

REG = registry.get()


# --- Across -------------------------------------------------------------------


ACROSS_OK = {
    "outputAmount": "999896330",
    "totalRelayFee": {"pct": "103670000000000", "total": "103670"},
    "estimatedFillTimeSec": 2,
    "isAmountTooLow": False,
    "limits": {"minDeposit": "500000", "maxDeposit": "5000000000000"},
}


def test_across_parses_suggested_fees(monkeypatch):
    monkeypatch.delenv("ACROSS_API_KEY", raising=False)
    seen = {}

    def get(url, headers):
        seen["url"], seen["headers"] = url, headers
        return ACROSS_OK

    q = Across(http_get=get).quote(REG, "USDC", "tempo", "base", 1000)
    params = dict(urllib.parse.parse_qsl(seen["url"].split("?", 1)[1]))
    assert params["originChainId"] == "4217" and params["destinationChainId"] == "8453"
    assert params["inputToken"] == REG.chains["tempo"].tokens["USDC"].address
    assert params["outputToken"] == REG.chains["base"].tokens["USDC"].address
    assert params["amount"] == "1000000000" and seen["headers"] == {}
    assert q.amount_out == pytest.approx(999.89633) and q.token_fee == pytest.approx(0.10367)
    assert q.eta_seconds == 2 and q.gas_units == REG.routing.gas_units["approve"] + REG.routing.gas_units["bridge"]
    assert q.origin_gas_usd is None and q.meta["relay_fee"] == pytest.approx(0.10367)


def test_across_solana_ids_key_and_limits(monkeypatch):
    monkeypatch.setenv("ACROSS_API_KEY", "k")
    seen = {}

    def get(url, headers):
        seen["url"], seen["headers"] = url, headers
        return {**ACROSS_OK, "limits": {"maxDeposit": "100000000"}}

    with pytest.raises(BridgeError, match="above this route's limit of 100"):
        Across(http_get=get).quote(REG, "USDC", "base", "solana", 1000)
    params = dict(urllib.parse.parse_qsl(seen["url"].split("?", 1)[1]))
    assert params["destinationChainId"] == "34268394551451" and params["recipient"] == REG.routing.quote_wallets["solana"]
    assert seen["headers"] == {"Authorization": "Bearer k"}


def test_across_errors():
    with pytest.raises(BridgeError, match="below the minimum"):
        Across(http_get=lambda u, h: {**ACROSS_OK, "isAmountTooLow": True}).quote(REG, "USDC", "base", "arbitrum", 0.1)

    def boom(u, h):
        raise httpjson.HttpError("HTTP 400: Unsupported route", 400)

    with pytest.raises(BridgeError, match="Across: HTTP 400: Unsupported route"):
        Across(http_get=boom).quote(REG, "USDC", "base", "arbitrum", 10)
    with pytest.raises(BridgeError, match="EURC isn't listed on Arbitrum"):
        Across(http_get=boom).quote(REG, "EURC", "base", "arbitrum", 10)


# --- Relay ---------------------------------------------------------------------


RELAY_OK = {
    "fees": {
        "gas": {"amount": "1200", "amountUsd": "0.0021"},
        "relayer": {"amount": "150000", "amountUsd": "0.15"},
    },
    "details": {
        "currencyOut": {"amount": "999850000", "currency": {"decimals": 6, "symbol": "USDC"}},
        "timeEstimate": 3,
    },
}


def test_relay_body_and_parse(monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", "rk")
    seen = {}

    def post(url, body, headers):
        seen.update(url=url, body=body, headers=headers)
        return RELAY_OK

    q = Relay(http_post=post).quote(REG, "USDC", "polygon", "solana", 1000)
    assert seen["body"]["originChainId"] == 137 and seen["body"]["destinationChainId"] == 792703809
    assert seen["body"]["tradeType"] == "EXACT_INPUT" and seen["body"]["amount"] == "1000000000"
    assert seen["body"]["recipient"] == REG.routing.quote_wallets["solana"]
    assert seen["body"]["user"] == REG.routing.quote_wallets["evm"]
    assert seen["headers"] == {"x-api-key": "rk"}
    assert q.amount_out == pytest.approx(999.85) and q.origin_gas_usd == pytest.approx(0.0021) and q.gas_units is None
    assert q.eta_seconds == 3 and q.meta["relayer_fee_usd"] == pytest.approx(0.15)


def test_relay_needs_key_hint(monkeypatch):
    monkeypatch.delenv("RELAY_API_KEY", raising=False)

    def post(url, body, headers):
        raise httpjson.HttpError("HTTP 401: unauthorized", 401)

    with pytest.raises(BridgeError, match="set RELAY_API_KEY"):
        Relay(http_post=post).quote(REG, "USDC", "base", "arbitrum", 10)
    with pytest.raises(BridgeError, match="unexpected response"):
        Relay(http_post=lambda u, b, h: {"details": {}}).quote(REG, "USDC", "base", "arbitrum", 10)


# --- LayerZero ----------------------------------------------------------------


LZ_OK = {
    "quotes": [
        {"id": "q1", "dstAmount": "998000000", "feeUsd": "0.9", "srcAmountUsd": "1000", "dstAmountUsd": "998.5",
         "routeSteps": [{"type": "STARGATE_V2_BUS"}], "duration": {"estimated": 180}},
        {"id": "q2", "dstAmount": "999500000", "feeUsd": "1.2", "srcAmountUsd": "1000", "dstAmountUsd": "999.5",
         "routeSteps": [{"type": "STARGATE_V2_TAXI"}], "duration": {"estimated": 60}},
    ]
}


def test_layerzero_picks_most_arriving_and_messaging_fee(monkeypatch):
    monkeypatch.setenv("LAYERZERO_API_KEY", "lz")
    seen = {}

    def post(url, body, headers):
        seen.update(body=body, headers=headers)
        return LZ_OK

    q = LayerZero(http_post=post).quote(REG, "USDC", "ethereum", "tempo", 1000)
    assert seen["body"]["srcChainKey"] == "ethereum" and seen["body"]["dstChainKey"] == "tempo"
    assert seen["body"]["dstTokenAddress"] == REG.chains["tempo"].tokens["USDC"].address
    assert seen["headers"] == {"x-api-key": "lz"}
    assert q.amount_out == pytest.approx(999.5) and q.route == "STARGATE_V2_TAXI"
    assert q.fee_usd == pytest.approx(1.2 - 0.5)  # feeUsd minus what's already taken from the tokens
    assert q.eta_seconds == 60


def test_layerzero_needs_key_and_handles_no_quotes(monkeypatch):
    monkeypatch.delenv("LAYERZERO_API_KEY", raising=False)
    with pytest.raises(BridgeError, match="set LAYERZERO_API_KEY"):
        LayerZero(http_post=lambda u, b, h: LZ_OK).quote(REG, "USDC", "base", "tempo", 10)
    monkeypatch.setenv("LAYERZERO_API_KEY", "lz")
    with pytest.raises(BridgeError, match="no quote"):
        LayerZero(http_post=lambda u, b, h: {"quotes": [], "error": "unsupported token"}).quote(REG, "USDC", "base", "tempo", 10)


def test_config_bridge_coverage():
    r = REG.routing
    assert r.bridge_covers("across", "USDC", "tempo", "base")
    assert not r.bridge_covers("across", "EURC", "base", "ethereum")
    assert r.coinbase_accepts("USDC", "arbitrum") and not r.coinbase_accepts("USDC", "tempo")
    assert not r.coinbase_accepts("EURC", "polygon")


# --- gas ------------------------------------------------------------------------


def test_gas_oracle_reads_each_price_once():
    gets, posts = [], []

    def get(url, h):
        gets.append(url)
        return {"price": "2500"}

    def post(url, body, h):
        posts.append(url)
        assert body["method"] == "eth_gasPrice"
        return {"result": hex(2 * 10**9)}

    g = GasOracle(REG, http_get=get, http_post=post)
    c = g.cost("base", 100_000)
    assert c.gas_token == "ETH" and c.native == pytest.approx(100_000 * 2e-9) and c.usd == pytest.approx(0.5)
    g.cost("base", 50_000)
    g.cost("arbitrum", 50_000)
    assert len(posts) == 2 and len(gets) == 1  # ETH-USD shared by Base and Arbitrum
    assert gets[0].endswith("/ETH-USD")


def test_gas_oracle_tempo_and_solana_and_rpc_env(monkeypatch):
    monkeypatch.setenv("TEMPO_RPC_URL", "https://my-tempo")
    urls = []

    def post(url, body, h):
        urls.append(url)
        return {"result": hex(6 * 10**8)}  # attodollars per gas at Tempo's floor

    g = GasOracle(REG, http_get=lambda u, h: {"price": "150"}, http_post=post)
    t = g.cost("tempo", 50_000)
    assert t.gas_token == "USD stablecoin" and t.usd == pytest.approx(50_000 * 6e8 / 1e18) and urls == ["https://my-tempo"]
    s = g.cost("solana", 999_999)
    assert s.units is None and s.usd == pytest.approx(REG.routing.solana_fee_lamports / 1e9 * 150)


def test_gas_oracle_errors():
    def down(*a):
        raise OSError("nope")

    g = GasOracle(REG, http_get=down, http_post=down)
    with pytest.raises(GasError, match="Base gas price unavailable"):
        g.cost("base", 1)
    with pytest.raises(GasError, match="SOL-USD price unavailable"):
        g.cost("solana", 1)
