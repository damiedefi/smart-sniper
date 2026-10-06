"""Smart Sniper: build the live dataset the browser UI runs on.

Every call here goes to CoinGecko API:
  1. trending pools on the chain           /onchain/networks/{chain}/trending_pools
  2. top traders for each trending token   /onchain/networks/{chain}/tokens/{token}/top_traders
     (ranked by realized PnL, highest first)
  3. each wallet's PnL across EVM chains   /onchain/wallets/{address}/pnl

The UI then applies the keep/cut rules in the browser, so moving a slider never costs an API call.
Those verdicts (bot, one-hit wonder, kept...) are this repo's own logic, not CoinGecko fields.
"""
import asyncio
import time

from core import config as core_config
from core import wallets as w
from core.client import CoinGeckoClient, CoinGeckoError, PlanRestrictedError

from . import config, scan
from .profile import wallet_networks

# Wallets with more trades than this in a single token are dropped before sampling: they're
# obviously automated and would only cost credits to profile.
HEAVY_TRADES_IN_ONE_TOKEN = 1500
# Base assets and stablecoins aren't interesting "still holding" signals.
NOT_HOLDINGS = {"WETH", "ETH", "USDC", "USDT", "USDG", "DAI", "WBTC", "USDC.E", "USDBC", "WBNB", "BNB", "WAVAX", "WPOL", "MATIC", "WMATIC", "CBBTC"}
MAX_CONCURRENT = 8


def compact_wallet(address: str, found_in: list[str], pnl_attrs: dict) -> dict:
    """One wallet in the compact shape the UI expects. Pure function, easy to test."""
    stats = pnl_attrs.get("token_stats") or []
    f = lambda v: w._f(v, 0.0) or 0.0  # noqa: E731
    sold = [f(s.get("realized_pnl_usd")) for s in stats if (s.get("total_sell_count") or 0) > 0]
    positive = [f(s.get("realized_pnl_usd")) for s in stats if f(s.get("realized_pnl_usd")) > 0]
    best = max((s for s in stats if f(s.get("realized_pnl_usd")) > 0), key=lambda s: f(s.get("realized_pnl_usd")), default=None)
    holds = [s for s in stats if f(s.get("unrealized_pnl_usd")) != 0 and (s.get("total_buy_count") or 0) > 0 and str(s.get("symbol") or "").upper() not in NOT_HOLDINGS]
    holds.sort(key=lambda s: -f(s.get("total_buy_usd")))
    return {
        "a": address,
        "s": ",".join(found_in),
        "r": round(sum(f(s.get("realized_pnl_usd")) for s in stats)),
        "c": round(max(positive) / sum(positive), 3) if positive else None,
        "wr": round(sum(1 for r in sold if r > 0) / len(sold), 3) if sold else None,
        "n": len(sold),
        "t": sum((s.get("total_buy_count") or 0) + (s.get("total_sell_count") or 0) for s in stats),
        "ch": sum(1 for n in (pnl_attrs.get("networks") or []) if (n.get("tokens") or 0) > 0),
        "tk": pnl_attrs.get("total_tokens") or len(stats),
        "bs": best.get("symbol") if best else None,
        "bu": round(f(best.get("realized_pnl_usd"))) if best else None,
        "h": [[s.get("symbol"), s.get("network"), round(f(s.get("total_buy_usd")))] for s in holds[:6]],
    }


def sample_evenly(items: list, k: int) -> list:
    """Take k items spread across the list instead of the first k (the head is full of bots)."""
    if len(items) <= k:
        return items
    step = len(items) / k
    return [items[int(i * step)] for i in range(k)]


async def build(client: CoinGeckoClient, chain: str = "robinhood", source: str = "trending_24h", n_tokens: int = 15, traders_per_token: int = 40, max_wallets: int = 150) -> dict:
    tokens = await scan._tokens_from_pools(client, chain, source, n_tokens)
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def traders(tok):
        async with sem:
            try:
                return tok, await client.top_traders(chain, tok["address"], n=traders_per_token)
            except PlanRestrictedError:
                raise
            except CoinGeckoError:
                return tok, []

    results = await asyncio.gather(*(traders(t) for t in tokens))
    slots = heavy = 0
    found: dict[str, list[str]] = {}
    for tok, rows in results:
        for t in rows:
            addr = str(t.get("address") or "").lower()
            if w.address_kind(addr) != "evm":
                continue
            slots += 1
            if (t.get("total_buy_count") or 0) + (t.get("total_sell_count") or 0) > HEAVY_TRADES_IN_ONE_TOKEN:
                heavy += 1
                continue
            found.setdefault(addr, [])
            if tok["symbol"] not in found[addr]:
                found[addr].append(tok["symbol"])

    sample = sample_evenly(list(found.items()), max_wallets)

    async def pnl(addr):
        async with sem:
            try:
                return await client.wallet_pnl(addr, wallet_networks(addr, chain), per_page=200, sort="total_buy_usd_desc")
            except PlanRestrictedError:
                raise
            except CoinGeckoError:
                return None

    pnls = await asyncio.gather(*(pnl(a) for a, _ in sample))
    wallets = [compact_wallet(a, s, p) for (a, s), p in zip(sample, pnls) if p]
    return {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "chain": core_config.CHAINS.get(chain, chain),
        "source": config.SOURCES.get(source, source),
        "tokens": [t["symbol"] for t in tokens],
        "slots": slots,
        "heavy": heavy,
        "unique": len(found),
        "wallets": wallets,
        "live": True,
        "credits_used": getattr(client, "credits_used", None),
    }
