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
    assert out["errors"] == []
    conn.close()


def test_cron_cycle_reports_errors(tmp_path):
    class Broken(Venue):
        name = "broken"

        def simulate(self, pair, token_in, token_out, amount_in):
            raise RuntimeError("pool reverted")

    out = service.cron_cycle(
        connect=lambda: storage.connect(tmp_path / "fx.db"),
        reference=REF,
        venues=[FakeVenue("ok"), Broken()],
        sizes=[1000, 5000],
    )
    assert out["quotes"] == 4 and out["failures"] == 4
    assert out["errors"] == ["EURC/USDC broken: RuntimeError: pool reverted (x4)"]


def test_cron_cycle_reports_reference_failure(tmp_path):
    out = service.cron_cycle(
        connect=lambda: storage.connect(tmp_path / "fx.db"),
        reference=StaticReference({}),
        venues=[FakeVenue()],
    )
    assert out["quotes"] == 0 and out["references"] == {}
    assert len(out["errors"]) == 1 and out["errors"][0].startswith("EURC/USDC reference: KeyError")


# --- outgoing HTTP ---------------------------------------------------------


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def read(self, *a):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_reference_request_sets_user_agent(monkeypatch):
    import urllib.request

    from execution_cost_tracker import reference

    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        return FakeResponse(b'{"date": "2026-10-02", "rates": {"USD": 1.1}}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    rate = reference.FrankfurterReference().mid("EUR/USD")
    assert rate.mid == 1.1
    assert seen["ua"] == reference.USER_AGENT and "Python-urllib" not in seen["ua"]


def test_reference_http_error_is_readable(monkeypatch):
    import io
    import urllib.error
    import urllib.request

    from execution_cost_tracker import reference

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b"blocked"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="403.*blocked"):
        reference.FrankfurterReference().mid("EUR/USD")


def test_zerox_request_sets_user_agent_and_keeps_headers(monkeypatch):
    import urllib.request

    from execution_cost_tracker.venues import zerox

    seen = {}

    def fake_urlopen(req, timeout=None):
        seen.update({k.lower(): v for k, v in req.header_items()})
        return FakeResponse(b'{"buyAmount": "1"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    zerox._http_get("https://api.0x.org/x", {"0x-api-key": "k", "0x-version": "v2"})
    assert seen["user-agent"].startswith("execution-cost-tracker")
    assert seen["0x-api-key"] == "k" and seen["0x-version"] == "v2"


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
