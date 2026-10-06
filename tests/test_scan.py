"""Scan tab: dedupe wallets across tokens, count how many tokens each shows up in, flag bots."""
from app import scan
from tests.conftest import FakeClient, make_pool


async def test_scan_dedupes_and_counts_wallets_across_tokens():
    client = FakeClient(
        pools=[make_pool("AAA", "AAA"), make_pool("BBB", "BBB")],
        top_traders_by_token={
            "AAA": [{"address": "0xWALLET1", "realized_pnl_usd": "100", "total_buy_count": 2, "total_sell_count": 1}],
            "BBB": [{"address": "0xwallet1", "realized_pnl_usd": "50", "total_buy_count": 1, "total_sell_count": 1}],
        },
    )
    result = await scan.scan(client, "solana", "trending_1h", n_tokens=2)
    assert len(result["candidates"]) == 1
    c = result["candidates"][0]
    assert c["tokens_seen_in"] == 2
    assert c["realized_seen_usd"] == 150


async def test_scan_flags_likely_bots_without_dropping_them():
    client = FakeClient(
        pools=[make_pool("AAA", "AAA")],
        top_traders_by_token={
            "AAA": [{"address": "0xBOT", "realized_pnl_usd": "10", "total_buy_count": 900, "total_sell_count": 900}],
        },
    )
    result = await scan.scan(client, "solana", "trending_1h", n_tokens=1)
    assert result["candidates"][0]["likely_bot"] is True


async def test_scan_ranks_non_bots_above_bots():
    client = FakeClient(
        pools=[make_pool("AAA", "AAA")],
        top_traders_by_token={
            "AAA": [
                {"address": "0xBOT", "realized_pnl_usd": "999999", "total_buy_count": 900, "total_sell_count": 900},
                {"address": "0xHUMAN", "realized_pnl_usd": "10", "total_buy_count": 2, "total_sell_count": 1},
            ]
        },
    )
    result = await scan.scan(client, "solana", "trending_1h", n_tokens=1)
    addresses = [c["address"] for c in result["candidates"]]
    assert addresses[0] == "0xHUMAN"
