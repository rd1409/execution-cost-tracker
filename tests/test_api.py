"""HTTP-level tests for api.py. Skipped when FastAPI isn't installed."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from execution_cost_tracker import api, guardrails, service  # noqa: E402


@pytest.fixture
def client():
    return TestClient(api.app)


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True}


def test_pairs(client):
    assert any(p["pair"] == "EURC/USDC" for p in client.get("/api/pairs").json())


def test_index_serves_front_end(client):
    r = client.get("/")
    assert r.status_code == 200 and "FX execution check" in r.text


def test_config(client):
    cfg = client.get("/api/config").json()
    assert any(p["pair"] == "EURC/USDC" for p in cfg["pairs"]) and cfg["limits"]["max_notional"] > 0


def test_quote_rejects_bad_input(client):
    assert client.post("/api/quote", json={"notional": 10**12}).status_code == 400
    assert client.post("/api/quote", json={"pair": "BTC/USDC"}).status_code == 400
    assert client.post("/api/quote", json={"side": "short"}).status_code == 400
    assert client.post("/api/quote", json={"destination_chain": "solana"}).status_code == 400
    assert client.post("/api/quote", json={"notional": -1}).status_code == 422


def test_quote_calls_guarded_service(client, monkeypatch):
    seen = {}

    def fake_guarded(params, client_id):
        seen["params"], seen["client"] = params, client_id
        return {"rows": []}

    monkeypatch.setattr(service, "guarded_quote", fake_guarded)
    r = client.post(
        "/api/quote",
        json={"notional": 5000, "venues": ["zerox"], "side": "buy"},
        headers={"X-Real-IP": "9.9.9.9"},
    )
    assert r.status_code == 200 and r.json() == {"rows": []}
    p = seen["params"]
    assert p.notional == 5000 and p.venues == ["zerox"] and p.side == "buy" and p.destination_chain == "base"
    assert seen["client"] == "9.9.9.9"


def test_quote_rate_limited_returns_429(client, monkeypatch):
    def limited(params, client_id):
        raise guardrails.RateLimited("client", 7)

    monkeypatch.setattr(service, "guarded_quote", limited)
    r = client.post("/api/quote", json={})
    assert r.status_code == 429 and r.headers["Retry-After"] == "7"
    assert r.json()["retry_after"] == 7 and "7s" in r.json()["detail"]


def test_cron_requires_secret(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret-value-123456")
    monkeypatch.setattr(service, "cron_cycle", lambda: {"run_id": "x"})
    assert client.get("/api/cron").status_code == 401
    assert client.get("/api/cron", headers={"Authorization": "Bearer nope"}).status_code == 401
    ok = client.get("/api/cron", headers={"Authorization": "Bearer s3cret-value-123456"})
    assert ok.status_code == 200 and ok.json() == {"run_id": "x"}


def test_tradingview_webhook_route(client, monkeypatch):
    seen = {}

    def fake(body, ip, secret):
        seen.update(body=body, ip=ip, secret=secret)
        return {"ok": True}

    monkeypatch.setenv("TRADINGVIEW_WEBHOOK_SECRET", "s")
    monkeypatch.setattr(service, "handle_tradingview_webhook", fake)
    r = client.post("/api/tradingview/webhook", content=b'{"price": 1.1}', headers={"X-Real-IP": "52.89.214.238"})
    assert r.status_code == 200 and seen == {"body": b'{"price": 1.1}', "ip": "52.89.214.238", "secret": "s"}

    def reject(body, ip, secret):
        raise service.WebhookError(403, "nope")

    monkeypatch.setattr(service, "handle_tradingview_webhook", reject)
    r = client.post("/api/tradingview/webhook", content=b"{}")
    assert r.status_code == 403 and r.json() == {"detail": "nope"}
