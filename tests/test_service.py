"""Offline tests for the web API's logic (service.py) and storage backends."""

import json
import sqlite3

import pytest

from execution_cost_tracker import Side, storage
from execution_cost_tracker import service
from execution_cost_tracker.reference import StaticReference
from execution_cost_tracker.venues import SwapResult, Venue

REF = StaticReference({"EUR/USD": 1.085})


class FakeVenue(Venue):
    def __init__(self, name="fake", bid=1.084, ask=1.086):
        self.name, self.bid, self.ask = name, bid, ask

    def simulate(self, pair, token_in, token_out, amount_in):
        amount = token_in.from_raw(amount_in)
        out = amount * self.bid if token_in is pair.base else amount / self.ask
        return SwapResult(token_out.to_raw(out), 120_000, {"fake": True})


def fake_factory(names):
    return [FakeVenue(n) for n in names]


# --- input validation ----------------------------------------------------


def test_quote_params_defaults_are_valid():
    p = service.QuoteParams()
    assert p.pair == "EURC/USDC" and p.notional > 0 and p.venues


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pair": "BTC/USDC"},
        {"notional": 0},
        {"notional": -5},
        {"notional": 10**12},
        {"venues": []},
        {"venues": ["nope"]},
    ],
)
def test_quote_params_rejects_bad_input(kwargs):
    with pytest.raises(ValueError):
        service.QuoteParams(**kwargs)


def test_max_notional_from_env(monkeypatch):
    monkeypatch.setenv("MAX_NOTIONAL", "500")
    with pytest.raises(ValueError, match="500"):
        service.QuoteParams(notional=501)
    assert service.QuoteParams(notional=500).notional == 500


# --- endpoints' logic ----------------------------------------------------


def test_list_pairs():
    pairs = service.list_pairs()
    eurc = next(p for p in pairs if p["pair"] == "EURC/USDC")
    assert eurc["chain"] == "base" and eurc["reference"] == "EUR/USD"
    assert eurc["base"]["symbol"] == "EURC" and eurc["base"]["decimals"] == 6
    assert set(eurc["venues"]) == {"uniswap_v3", "uniswap_v4", "aerodrome", "zerox"}


def test_quote_returns_json_ready_result():
    params = service.QuoteParams(notional=25_000, venues=["uniswap_v3", "zerox"])
    out = service.quote(params, reference=REF, venue_factory=fake_factory)
    json.dumps(out)  # must serialise as-is
    assert out["references"]["EURC/USDC"]["mid"] == 1.085
    assert {r["venue"] for r in out["rows"]} == {"uniswap_v3", "zerox"}
    assert all(r["size"] == 25_000 for r in out["rows"])
    assert len(out["quotes"]) == 4 and out["failures"] == []
    assert {q["side"] for q in out["quotes"]} == {Side.BUY.value, Side.SELL.value}


@pytest.mark.parametrize(
    "header,secret,ok",
    [
        ("Bearer s3cret-value-123456", "s3cret-value-123456", True),
        ("Bearer wrong", "s3cret-value-123456", False),
        ("s3cret-value-123456", "s3cret-value-123456", False),
        (None, "s3cret-value-123456", False),
        ("Bearer ", "", False),
        ("Bearer None", None, False),
    ],
)
def test_check_cron_auth(header, secret, ok):
    assert service.check_cron_auth(header, secret) is ok


def test_cron_cycle_stores_and_closes(tmp_path):
    path = tmp_path / "fx.db"
    out = service.cron_cycle(
        connect=lambda: storage.connect(path),
        reference=REF,
        venues=[FakeVenue("a"), FakeVenue("b")],
        sizes=[1000, 5000],
    )
    assert out["quotes"] == 2 * 2 * 2 and out["failures"] == 0
    assert out["references"] == {"EURC/USDC": 1.085}
    conn = storage.connect(path)
    assert len(storage.load_quotes(conn, run_id=out["run_id"])) == 8
    conn.close()


# --- storage backends ----------------------------------------------------


def test_storage_refuses_local_file_on_vercel(monkeypatch, tmp_path):
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.setenv("VERCEL", "1")
    with pytest.raises(RuntimeError, match="TURSO"):
        storage.connect(tmp_path / "x.db")


def test_storage_uses_turso_when_configured(monkeypatch, tmp_path):
    """turso_serverless is swapped for sqlite3 to check the wiring offline."""
    import sys
    import types

    calls = {}

    def fake_connect(url, auth_token=None):
        calls["url"], calls["token"] = url, auth_token
        return sqlite3.connect(str(tmp_path / "turso.db"))

    monkeypatch.setitem(sys.modules, "turso_serverless", types.SimpleNamespace(connect=fake_connect))
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://db.example")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok")
    conn = storage.connect()
    assert calls == {"url": "libsql://db.example", "token": "tok"}
    assert storage.load_quotes(conn) == []
    conn.close()
