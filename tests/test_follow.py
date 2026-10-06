"""Follow engine: mirrors a wallet's buy/sell into the paper engine, sized to the user's budget."""
from app.follow import FollowEngine


class OneShotClient:
    """Returns a canned set of trades once, then nothing (as if the wallet went quiet)."""

    def __init__(self, trades):
        self._trades = trades
        self._served = False
        self.analyst = True

    async def wallet_trades(self, network, address, max_pages=1):
        if self._served:
            return []
        self._served = True
        return self._trades


ASSUMPTIONS = {"slippage_bps": 0, "fee_bps": 0, "starting_cash": 100, "max_position_pct": 0.3, "cooldown_s": 0}


async def test_poll_once_mirrors_a_buy_sized_to_budget():
    trades = [{"kind": "buy", "to_token_address": "TOKEN", "to_token_amount": 1000, "volume_in_usd": 50_000, "block_timestamp": 1_900_000_000}]
    client = OneShotClient(trades)
    engine = FollowEngine(client, "solana", ["0xW"], budget_usd=100, assumptions=ASSUMPTIONS, poll_s=1)
    decisions = await engine.poll_once()
    assert decisions[0]["action"] == "buy"
    # sized to budget * max_position_pct, never the wallet's actual $50,000 trade
    assert engine.portfolio.positions["TOKEN"].cost_usd == 30


async def test_poll_once_is_idempotent_across_polls():
    trades = [{"kind": "buy", "to_token_address": "TOKEN", "to_token_amount": 10, "volume_in_usd": 20, "block_timestamp": 1_900_000_000}]
    client = OneShotClient(trades)
    engine = FollowEngine(client, "solana", ["0xW"], budget_usd=100, assumptions=ASSUMPTIONS, poll_s=1)
    first = await engine.poll_once()
    second = await engine.poll_once()  # the client returns [] the second time, like a wallet with no new trades
    assert len(first) == 1
    assert len(second) == 0


async def test_status_reports_latency_note():
    client = OneShotClient([])
    engine = FollowEngine(client, "solana", [], budget_usd=100, assumptions=ASSUMPTIONS, poll_s=30)
    status = engine.status()
    assert status["poll_s"] == 30
    assert "latency_note" in status
