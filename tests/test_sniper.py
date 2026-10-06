"""Offline tests for Smart Sniper's live data builder (app/sniper.py) and server (app/sniper_server.py)."""
from fastapi.testclient import TestClient

from app import sniper, sniper_server

A = "0x" + "a" * 40
B = "0x" + "b" * 40
BOT = "0x" + "c" * 40


def stat(symbol, realized, sells=1, buys=1, unrealized=0, buy_usd=1000, network="robinhood"):
    return {"symbol": symbol, "network": network, "address": symbol.lower(), "realized_pnl_usd": realized, "unrealized_pnl_usd": unrealized, "total_buy_usd": buy_usd, "total_buy_count": buys, "total_sell_count": sells}


PNL_A = {"token_stats": [stat("X", 900), stat("Y", 100), stat("Z", -50), stat("NVDA", 0, sells=0, unrealized=20), stat("WETH", 0, sells=0, unrealized=5)], "networks": [{"id": "robinhood", "tokens": 4}, {"id": "base", "tokens": 1}], "total_tokens": 5}


def test_compact_wallet_shape_and_math():
    c = sniper.compact_wallet(A, ["TOK"], PNL_A)
    assert c["a"] == A and c["s"] == "TOK"
    assert c["r"] == 950
    assert c["c"] == 0.9  # 900 of 1000 positive profit came from X
    assert c["wr"] == round(2 / 3, 3)
    assert c["n"] == 3 and c["ch"] == 2 and c["tk"] == 5
    assert c["bs"] == "X" and c["bu"] == 900
    assert [h[0] for h in c["h"]] == ["NVDA"]  # base assets like WETH are skipped


def test_sample_evenly_spreads_across_list():
    assert sniper.sample_evenly(list(range(10)), 5) == [0, 2, 4, 6, 8]
    assert sniper.sample_evenly([1, 2], 5) == [1, 2]


class FakeClient:
    credits_used = 0

    async def trending_pools(self, network, duration="1h", n=20):
        return [{"attributes": {"address": "pool1", "name": "TOK / USDG"}, "relationships": {"base_token": {"data": {"id": f"{network}_tok1"}}}}]

    async def top_traders(self, network, token, n=25, sort="realized_pnl_usd_desc"):
        return [
            {"address": A, "total_buy_count": 3, "total_sell_count": 2},
            {"address": B, "total_buy_count": 1, "total_sell_count": 1},
            {"address": BOT, "total_buy_count": 2000, "total_sell_count": 2000},
        ]

    async def wallet_pnl(self, address, networks, per_page=None, sort=None):
        return PNL_A


async def test_build_drops_heavy_traders_and_counts_slots():
    d = await sniper.build(FakeClient(), "robinhood", "trending_24h", n_tokens=5, traders_per_token=10, max_wallets=50)
    assert d["live"] is True
    assert d["slots"] == 3 and d["heavy"] == 1 and d["unique"] == 2
    assert {w["a"] for w in d["wallets"]} == {A, B}
    assert d["tokens"] == ["TOK"]


def test_server_serves_ui_and_snapshot(monkeypatch):
    async def fake_build(client, chain, source, max_wallets=150):
        return {"at": "2026-10-05T00:00:00Z", "chain": "Robinhood Chain", "source": "Trending (24h)", "tokens": ["TOK"], "slots": 1, "heavy": 0, "unique": 1, "wallets": [], "live": True}

    monkeypatch.setattr(sniper, "build", fake_build)
    monkeypatch.setattr(sniper_server, "CoinGeckoClient", FakeClose)
    sniper_server._cache.clear()
    with TestClient(sniper_server.app) as tc:
        r = tc.get("/")
        assert r.status_code == 200 and "Smart Sniper" in r.text
        r = tc.get("/api/snapshot")
        assert r.status_code == 200 and r.json()["live"] is True
        assert tc.get("/api/snapshot?source=nope").status_code == 400


class FakeClose:
    async def close(self):
        pass


async def test_snapshot_job_writes_data_file(monkeypatch, tmp_path):
    from app import snapshot_job

    async def fake_build(client, chain, source, max_wallets=150):
        return {"at": "x", "chain": "Robinhood Chain", "source": "Trending (24h)", "tokens": [], "slots": 0, "heavy": 0, "unique": 0, "wallets": [{"a": str(i)} for i in range(20)], "live": True}

    monkeypatch.setenv("COINGECKO_API_KEY", "test")
    monkeypatch.setattr(sniper, "build", fake_build)
    monkeypatch.setattr(snapshot_job, "CoinGeckoClient", FakeClose)
    monkeypatch.setattr(snapshot_job, "OUT", tmp_path / "data.json")
    assert await snapshot_job.main() == 0
    import json
    d = json.loads((tmp_path / "data.json").read_text())
    assert d["auto"] is True and d["live"] is False and len(d["wallets"]) == 20


async def test_snapshot_job_keeps_old_file_on_thin_result(monkeypatch, tmp_path):
    from app import snapshot_job

    async def thin(client, chain, source, max_wallets=150):
        return {"wallets": [], "credits_used": 1}

    monkeypatch.setenv("COINGECKO_API_KEY", "test")
    monkeypatch.setattr(sniper, "build", thin)
    monkeypatch.setattr(snapshot_job, "CoinGeckoClient", FakeClose)
    monkeypatch.setattr(snapshot_job, "OUT", tmp_path / "data.json")
    assert await snapshot_job.main() == 1
    assert not (tmp_path / "data.json").exists()
