"""Plan-aware behavior: a Demo key must show a locked card instead of an infinite spinner.

Startup calls core.plan.probe_capabilities() once, which is a live network call -- so every test
here patches it out before the TestClient's lifespan runs.
"""
from fastapi.testclient import TestClient

from app import server

DEMO_CAPS = {"paid": False, "analyst": False, "websocket": False, "upgrade_url": "https://www.coingecko.com/en/api/pricing"}
PAID_CAPS = {"paid": True, "analyst": True, "websocket": True, "upgrade_url": "https://www.coingecko.com/en/api/pricing"}


def _client_with_caps(monkeypatch, caps):
    async def fake_probe(client):
        return caps

    monkeypatch.setattr(server, "probe_capabilities", fake_probe)
    return TestClient(server.app)


def test_scan_locked_on_demo_plan(monkeypatch):
    with _client_with_caps(monkeypatch, DEMO_CAPS) as client:
        resp = client.post("/api/scan", json={"chain": "solana", "source": "trending_1h", "n_tokens": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["locked"] is True
    assert "upgrade_url" in body


def test_wallets_profile_locked_on_demo_plan(monkeypatch):
    with _client_with_caps(monkeypatch, DEMO_CAPS) as client:
        resp = client.post("/api/wallets/profile", json={"chain": "solana", "addresses": ["0xabc"], "budget": 100})
    assert resp.json()["locked"] is True


def test_follow_start_locked_on_demo_plan(monkeypatch):
    with _client_with_caps(monkeypatch, DEMO_CAPS) as client:
        resp = client.post("/api/follow/start", json={"chain": "solana", "addresses": ["0xabc"], "budget": 100})
    assert resp.json()["locked"] is True


def test_capabilities_endpoint_never_hangs(monkeypatch):
    with _client_with_caps(monkeypatch, PAID_CAPS) as client:
        resp = client.get("/api/capabilities")
    assert resp.status_code == 200
    assert resp.json()["analyst"] is True
