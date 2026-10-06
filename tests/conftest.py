"""Test fixtures: a fake CoinGeckoClient that never touches the network."""
import pytest


class FakeClient:
    """Stands in for core.client.CoinGeckoClient in tests: canned responses, no network."""

    def __init__(self, pools=None, top_traders_by_token=None, wallet_trades_by_addr=None, wallet_pnl_by_addr=None, wallet_balances_by_addr=None):
        self._pools = pools or []
        self._top_traders = top_traders_by_token or {}
        self._wallet_trades = wallet_trades_by_addr or {}
        self._wallet_pnl = wallet_pnl_by_addr or {}
        self._wallet_balances = wallet_balances_by_addr or {}
        self.credits_used = 0
        self.analyst = True

    async def trending_pools(self, network, duration="1h", n=20):
        return self._pools[:n]

    async def new_pools(self, network, n=20):
        return self._pools[:n]

    async def megafilter(self, **filters):
        return self._pools

    async def top_traders(self, network, token, n=25, sort="realized_pnl_usd_desc"):
        return self._top_traders.get(token, [])

    async def wallet_trades(self, network, address, max_pages=10):
        return self._wallet_trades.get(address, [])

    async def wallet_pnl(self, address, networks):
        return self._wallet_pnl.get(address, {})

    async def wallet_balances(self, address, networks):
        return self._wallet_balances.get(address, {})


def make_pool(address, symbol):
    return {
        "attributes": {"address": f"pool_{address}", "name": f"{symbol} / USDC"},
        "relationships": {"base_token": {"data": {"id": f"solana_{address}"}}},
    }


@pytest.fixture
def fake_client():
    return FakeClient()
