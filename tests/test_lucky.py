"""Offline tests for app/lucky.py: the verdict rules and the end-to-end run on canned data."""
from pathlib import Path

from app import lucky
from core import wallets as w

from .conftest import FakeClient, make_pool

EVM_A = "0x" + "a" * 40
EVM_B = "0x" + "b" * 40
EVM_C = "0x" + "c" * 40
EVM_D = "0x" + "d" * 40
EVM_E = "0x" + "e" * 40


def stat(symbol, realized, sells=1, buy=1000.0, unrealized=0, buys=1):
    return {"symbol": symbol, "network": "robinhood", "address": symbol.lower(), "realized_pnl_usd": realized, "unrealized_pnl_usd": unrealized, "total_buy_usd": buy, "total_sell_usd": buy + max(realized, 0), "total_buy_count": buys, "total_sell_count": sells}


def pnl(stats):
    return {"token_stats": stats, "total_tokens": len(stats), "total_realized_pnl_usd": sum(s["realized_pnl_usd"] for s in stats), "total_unrealized_pnl_usd": 0, "networks": []}


# a consistent winner: 6 closed positions, 5 green, profit spread out, still holding NVDA
CONSISTENT = pnl([stat("A", 900), stat("B", 800), stat("C", 700), stat("D", 600), stat("E", 500), stat("F", -200), stat("NVDA", 0, sells=0, unrealized=120), stat("WETH", 0, sells=0, unrealized=50)])
CONSISTENT_2 = pnl([stat("G", 400), stat("H", 350), stat("I", 300), stat("J", 250), stat("K", 200), stat("NVDA", 0, sells=0, unrealized=40, buy=500)])
# a bot: tens of thousands of trades
BOT = pnl([stat(f"T{i}", 1000, sells=3000, buys=3000) for i in range(6)])
# a one-hit wonder: big profit, almost all from one token
ONE_HIT = pnl([stat("MOON", 20000), stat("B", 300), stat("C", 200), stat("D", -100), stat("E", 100), stat("F", 50)])
# too few closed positions to judge
TOO_FEW = pnl([stat("A", 5000), stat("B", 4000)])


def test_consistent_wallet_is_kept():
    v = lucky.verdict(w.pnl_features(CONSISTENT))
    assert v["keep"] and v["check"] == "pass"


def test_one_hit_wonder_is_cut():
    v = lucky.verdict(w.pnl_features(ONE_HIT))
    assert not v["keep"] and v["check"] == "one_hit"
    assert "%" in v["reason"]


def test_too_few_positions_is_cut():
    v = lucky.verdict(w.pnl_features(TOO_FEW))
    assert not v["keep"] and v["check"] == "too_few"


def test_bot_is_cut():
    v = lucky.verdict(w.pnl_features(BOT))
    assert not v["keep"] and v["check"] == "bot"


def test_holdings_skip_base_assets():
    syms = [h["symbol"] for h in lucky.holdings(CONSISTENT)]
    assert syms == ["NVDA"]


def test_ideas_need_two_kept_wallets():
    rows = [
        {"verdict": {"keep": True}, "holdings": lucky.holdings(CONSISTENT)},
        {"verdict": {"keep": True}, "holdings": lucky.holdings(CONSISTENT_2)},
        {"verdict": {"keep": False}, "holdings": lucky.holdings(CONSISTENT)},
    ]
    out = lucky.ideas(rows)
    assert len(out) == 1 and out[0]["symbol"] == "NVDA" and out[0]["wallets"] == 2


def test_no_data_is_cut():
    assert lucky.verdict({"available": False})["check"] == "no_data"


def test_rules_are_editable():
    loose = {**lucky.RULES, "max_profit_concentration": 0.99}
    assert lucky.verdict(w.pnl_features(ONE_HIT), loose)["keep"]


def test_best_token_picks_largest_realized():
    assert lucky.best_token(ONE_HIT)["symbol"] == "MOON"


class LuckyFakeClient(FakeClient):
    async def wallet_pnl(self, address, networks, per_page=None, sort=None):
        return self._wallet_pnl.get(address, {})


async def test_end_to_end_run_and_report(tmp_path: Path):
    traders = [{"address": a, "realized_pnl_usd": 100, "total_buy_usd": 100, "total_buy_count": 1, "total_sell_count": 1} for a in (EVM_A, EVM_B, EVM_C, EVM_D, EVM_E)]
    client = LuckyFakeClient(
        pools=[make_pool("tok1", "TOK")],
        top_traders_by_token={"tok1": traders},
        wallet_pnl_by_addr={EVM_A: CONSISTENT, EVM_B: ONE_HIT, EVM_C: TOO_FEW, EVM_D: BOT, EVM_E: CONSISTENT_2},
    )
    result = await lucky.run(client, "robinhood", "trending_24h", max_wallets=10, n_tokens=5)
    s = result["summary"]
    assert s["wallets_checked"] == 5
    assert s["kept"] == 2 and s["one_hit_wonders"] == 1 and s["bots"] == 1
    assert result["ideas"][0]["symbol"] == "NVDA"
    out = tmp_path / "report.html"
    lucky.build_report(result, out)
    body = out.read_text()
    assert "Smart Sniper" in body and "LOOKS GOOD, GETS CUT" in body and "<svg" in body
    assert "WHERE THE KEPT WALLETS ARE SITTING" in body
