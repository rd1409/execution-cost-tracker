"""HTTP-level tests for api.py. Skipped when FastAPI isn't installed."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from execution_cost_tracker import api, service  # noqa: E402


@pytest.fixture
def client():
    return TestClient(api.app)


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True}


def test_pairs(client):
    assert any(p["pair"] == "EURC/USDC" for p in client.get("/api/pairs").json())


def test_quote_rejects_bad_input(client):
    assert client.post("/api/quote", json={"notional": 10**12}).status_code == 400
    assert client.post("/api/quote", json={"pair": "BTC/USDC"}).status_code == 400
    assert client.post("/api/quote", json={"notional": -1}).status_code == 422


def test_quote_calls_service(client, monkeypatch):
    seen = {}

    def fake_quote(params):
        seen["params"] = params
        return {"rows": []}

    monkeypatch.setattr(service, "quote", fake_quote)
    r = client.post("/api/quote", json={"notional": 5000, "venues": ["zerox"]})
    assert r.status_code == 200 and r.json() == {"rows": []}
    assert seen["params"].notional == 5000 and seen["params"].venues == ["zerox"]


def test_cron_requires_secret(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret-value-123456")
    monkeypatch.setattr(service, "cron_cycle", lambda: {"run_id": "x"})
    assert client.get("/api/cron").status_code == 401
    assert client.get("/api/cron", headers={"Authorization": "Bearer nope"}).status_code == 401
    ok = client.get("/api/cron", headers={"Authorization": "Bearer s3cret-value-123456"})
    assert ok.status_code == 200 and ok.json() == {"run_id": "x"}
