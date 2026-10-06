"""Wallet profiling: the Radar's per-row profile, streamed, and the full Wallet Profile page.

Calls per wallet (Radar row): PnL across every chain in the wallet's VM family (1), its latest trades
on the scanned chain (1), and multi-chain balances where the chain has them (1). Results are cached
for 30 minutes, so re-profiling, opening the profile page, or auto-picking follows costs nothing extra.

Chains are never hard-limited here. If an endpoint doesn't cover a chain, the API answers with an
error and that one section comes back as {"status": "unavailable"}; the UI shows a one-line notice.
"""
import asyncio
import time

from core import config as core_config
from core import wallets as w
from core.client import CoinGeckoClient, CoinGeckoError, PlanRestrictedError

from . import scoring, tokens

CACHE_TTL_S = 30 * 60
TRADES_PER_PAGE = 200
_cache: dict[str, tuple[float, dict]] = {}


def wallet_networks(address: str, chain: str) -> list[str]:
    """The `networks` scope for the multi-chain wallet endpoints: the whole EVM set for 0x wallets."""
    kind = w.address_kind(address)
    if kind == "evm" and chain != "solana":
        nets = list(core_config.WALLET_EVM_NETWORKS)
        if chain not in nets:
            nets.append(chain)
        return nets
    if kind == "solana":
        return ["solana"] if chain in ("solana", "auto", "", None) else [chain]
    return [chain]


async def _section(coro, skip: bool = False):
    """Runs one endpoint call and returns (status, data): ok | unavailable | locked | error."""
    if skip:
        return "unavailable", None
    try:
        return "ok", await coro
    except PlanRestrictedError:
        return "locked", None
    except CoinGeckoError as e:
        return ("unavailable" if e.status in (400, 404, 422) else "error"), None


async def _fetch_core(client: CoinGeckoClient, chain: str, address: str) -> dict:
    """PnL + trades + balances for one wallet, cached. Each section carries its own status."""
    key = f"{chain}:{address.lower()}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_S:
        return hit[1]
    caps = core_config.wallet_caps(chain)
    nets = wallet_networks(address, chain)
    (pnl_s, pnl), (trd_s, trades), (bal_s, bal) = await asyncio.gather(
        _section(client.wallet_pnl(address, nets, per_page=200, sort="total_buy_usd_desc"), skip=not caps.get("pnl", True)),
        _section(client.wallet_trades(chain, address, max_pages=1, per_page=TRADES_PER_PAGE), skip=not caps.get("trades", True)),
        _section(client.wallet_balances(address, nets, per_page=100, value_usd_min=1), skip=not caps.get("balances", True)),
    )
    data = {
        "status": {"pnl": pnl_s, "trades": trd_s, "balances": bal_s},
        "pnl": pnl or {},
        "trades": trades or [],
        "balances": bal or {},
    }
    if pnl_s != "error" and trd_s != "error":
        _cache[key] = (time.time(), data)
    return data


def _summary(address: str, chain: str, raw: dict, budget_usd: float) -> dict:
    """The fields a Radar row / follow card needs, with no raw trade lists attached."""
    scored = scoring.profile_wallet(raw["pnl"], raw["trades"], budget_usd, balances_attrs=raw["balances"] if raw["status"]["balances"] == "ok" else None, address=address)
    scored.pop("closed_trades", None)
    # Keep the detailed feature objects for the drawer, but also expose the values
    # the scan table/follow cards need directly. The earlier UI only received the
    # nested objects, which made valid API P&L and win-rate data render as dashes.
    pnl = scored.get("pnl_features") or {}
    matched = scored.get("match_metrics") or {}
    portfolio = scored.get("portfolio") or {}
    activity = scored.get("activity") or {}
    return {
        "address": address,
        "chain": chain,
        "status": raw["status"],
        "realized_pnl_usd": matched.get("realized_pnl_usd", pnl.get("lifetime_realized_pnl_usd")),
        "unrealized_pnl_usd": pnl.get("lifetime_unrealized_pnl_usd"),
        "total_pnl_usd": (
            None
            if pnl.get("pnl_reliable") is False
            else round((matched.get("realized_pnl_usd") or pnl.get("lifetime_realized_pnl_usd") or 0) + (pnl.get("lifetime_unrealized_pnl_usd") or 0), 2)
        ),
        "pnl_reliable": pnl.get("pnl_reliable", True),
        "win_rate": matched.get("win_rate") if matched.get("trades") else pnl.get("win_rate_tokens"),
        "trades": matched.get("trades") or pnl.get("total_buys", 0) + pnl.get("total_sells", 0),
        "active_chains": pnl.get("active_networks") or [],
        "portfolio_value_usd": portfolio.get("portfolio_value_usd"),
        "first_seen_ts": activity.get("first_seen_ts"),
        "last_seen_ts": activity.get("last_seen_ts"),
        "style": scored.get("style"),
        "tags": scored.get("tags") or [],
        "days_since_last_trade": activity.get("days_since_last_trade"),
        **scored,
    }


async def profile_one(client: CoinGeckoClient, chain: str, address: str, budget_usd: float) -> dict:
    """One wallet, scored: label, skill, copyability at `budget_usd`, style, tags, lifetime PnL, portfolio."""
    raw = await _fetch_core(client, chain, address)
    return _summary(address, chain, raw, budget_usd)


async def profile_candidates(client: CoinGeckoClient, chain: str, addresses: list[str], budget_usd: float) -> list[dict]:
    """Profiles every candidate address (default 30) concurrently."""
    results = await asyncio.gather(*(profile_one(client, chain, a, budget_usd) for a in addresses), return_exceptions=True)
    out = []
    for addr, r in zip(addresses, results):
        if isinstance(r, Exception):
            out.append({"address": addr, "chain": chain, "error": str(r)[:200]})
        else:
            out.append(r)
    return out


async def stream_profiles(client: CoinGeckoClient, chain: str, addresses: list[str], budget_usd: float):
    """Yields ("start"|"wallet"|"progress"|"done", data) as each wallet finishes, for the Radar's live rows."""
    t0 = time.perf_counter()
    credits0 = client.credits_used
    yield "start", {"chain": chain, "total": len(addresses)}
    queue: asyncio.Queue = asyncio.Queue()

    async def one(addr: str):
        try:
            await queue.put(await profile_one(client, chain, addr, budget_usd))
        except Exception as e:  # one bad wallet never stops the stream
            await queue.put({"address": addr, "chain": chain, "error": type(e).__name__})

    tasks = [asyncio.create_task(one(a)) for a in addresses]
    try:
        for done in range(1, len(addresses) + 1):
            row = await queue.get()
            yield "wallet", row
            yield "progress", {"done": done, "total": len(addresses), "elapsed_ms": round((time.perf_counter() - t0) * 1000), "credits": client.credits_used - credits0}
    finally:
        for t in tasks:
            t.cancel()
    yield "done", {"elapsed_ms": round((time.perf_counter() - t0) * 1000), "credits": client.credits_used - credits0}


# ---- the full Wallet Profile page ----


async def detect_chain(client: CoinGeckoClient, address: str) -> str:
    """The chain a wallet is most active on, from its PnL per network. Solana for base58 addresses."""
    kind = w.address_kind(address)
    if kind == "solana":
        return "solana"
    try:
        pnl = await client.wallet_pnl(address, list(core_config.WALLET_EVM_NETWORKS), per_page=200, sort="total_buy_usd_desc")
    except CoinGeckoError:
        return "eth"
    return w.primary_network(pnl, "eth")


def _holdings(bal: dict) -> list[dict]:
    items = bal.get("balances") or []
    total = sum((w._f(i.get("value_usd"), 0.0) or 0) for i in items) or 1
    out = []
    for i in items:
        value = w._f(i.get("value_usd"), 0.0) or 0
        tokens.remember(i.get("network"), i.get("address"), i.get("symbol"), i.get("name"))
        out.append(
            {
                "network": i.get("network"),
                "address": i.get("address"),
                "symbol": i.get("symbol"),
                "name": i.get("name"),
                "native": i.get("token_type") == "native",
                "coin_id": i.get("coingecko_coin_id"),
                "balance": w._f(i.get("balance")),
                "price_usd": w._f(i.get("price_usd")),
                "value_usd": value,
                "share": round(value / total, 4),
                "change_24h": w._f(i.get("h24_price_change_percentage")),
            }
        )
    return sorted(out, key=lambda h: -h["value_usd"])


def _performance(pnl: dict) -> list[dict]:
    out = []
    for s in pnl.get("token_stats") or []:
        tokens.remember(s.get("network"), s.get("address"), s.get("symbol"), s.get("name"))
        realized = w._f(s.get("realized_pnl_usd"), 0.0) or 0
        unrealized = w._f(s.get("unrealized_pnl_usd"), 0.0) or 0
        bought = w._f(s.get("total_buy_usd"), 0.0) or 0
        out.append(
            {
                "network": s.get("network"),
                "address": s.get("address"),
                "symbol": s.get("symbol"),
                "name": s.get("name"),
                "realized_pnl_usd": realized,
                "unrealized_pnl_usd": unrealized,
                "total_pnl_usd": realized + unrealized,
                "roi": round((realized + unrealized) / bought, 3) if bought else None,
                "bought_usd": bought,
                "sold_usd": w._f(s.get("total_sell_usd"), 0.0) or 0,
                "buys": s.get("total_buy_count") or 0,
                "sells": s.get("total_sell_count") or 0,
                "avg_buy_usd": w._f(s.get("average_buy_price_usd")),
                "avg_sell_usd": w._f(s.get("average_sell_price_usd")),
            }
        )
    return sorted(out, key=lambda x: -abs(x["total_pnl_usd"]))


def _trades(chain: str, rows: list[dict]) -> list[dict]:
    out = []
    for t in rows[:150]:
        out.append(
            {
                "ts": t.get("block_timestamp"),
                "kind": t.get("kind"),
                "usd": w._f(t.get("volume_in_usd"), 0.0) or 0,
                "dex": t.get("pool_dex"),
                "pool": t.get("pool_address"),
                "tx": t.get("tx_hash"),
                "from_token": t.get("from_token_address"),
                "to_token": t.get("to_token_address"),
                "from_amount": w._f(t.get("from_token_amount")),
                "to_amount": w._f(t.get("to_token_amount")),
                "network": chain,
            }
        )
    return out


def _transfers(address: str, rows: list[dict]) -> list[dict]:
    me = address.lower()
    out = []
    for t in rows:
        frm, to = t.get("from_address") or "", t.get("to_address") or ""
        out.append(
            {
                "ts": t.get("block_timestamp"),
                "direction": t.get("direction"),
                "symbol": t.get("symbol"),
                "token": t.get("token_address"),
                "amount": w._f(t.get("amount")),
                "counterparty": to if frm.lower() == me else frm,
                "tx": t.get("tx_hash"),
            }
        )
    return out


def _by_chain(pnl_feat: dict, bal: dict) -> list[dict]:
    rows: dict[str, dict] = {}
    for net, v in (pnl_feat.get("pnl_by_network") or {}).items():
        rows[net] = {"network": net, **v, "balance_usd": 0.0, "holdings": 0}
    for n in bal.get("networks") or []:
        r = rows.setdefault(n.get("network"), {"network": n.get("network"), "realized_pnl_usd": 0, "unrealized_pnl_usd": 0, "tokens": 0})
        r["balance_usd"] = w._f(n.get("value_usd"), 0.0) or 0
        r["holdings"] = n.get("holdings") or 0
    out = [r for r in rows.values() if r.get("tokens") or r.get("balance_usd", 0) >= 1]
    return sorted(out, key=lambda r: -(abs(r.get("realized_pnl_usd") or 0) + (r.get("balance_usd") or 0)))


async def wallet_page(client: CoinGeckoClient, address: str, chain: str, budget_usd: float) -> dict:
    """Everything the Wallet Profile page shows. `chain` may be "auto" (detected from PnL)."""
    if chain in ("auto", "", None):
        chain = await detect_chain(client, address)
    caps = core_config.wallet_caps(chain)
    raw = await _fetch_core(client, chain, address)
    tr_status, transfers = await _section(client.wallet_transfers(chain, address, max_pages=1, per_page=50), skip=not caps.get("transfers", True))
    summary = _summary(address, chain, raw, budget_usd)

    holdings = _holdings(raw["balances"]) if raw["status"]["balances"] == "ok" else []
    performance = _performance(raw["pnl"])
    trades = _trades(chain, raw["trades"])
    transfer_rows = _transfers(address, transfers or [])

    pairs = {(h["network"], h["address"]) for h in holdings[:40] if h.get("address")}
    pairs |= {(p["network"], p["address"]) for p in performance[:40] if p.get("address")}
    pairs |= {(chain, t[k]) for t in trades[:40] for k in ("from_token", "to_token") if t.get(k)}
    pairs |= {(chain, t["token"]) for t in transfer_rows[:30] if t.get("token")}
    meta = await tokens.resolve(client, pairs)

    def m(net, addr):
        return meta.get(f"{net}:{(addr or '').lower()}") or tokens.get(net, addr)

    for h in holdings:
        h["image"] = m(h["network"], h["address"]).get("image")
    for p in performance:
        p["image"] = m(p["network"], p["address"]).get("image")
    for t in trades:
        f, to = m(chain, t["from_token"]), m(chain, t["to_token"])
        t.update(from_symbol=f["symbol"], from_image=f["image"], to_symbol=to["symbol"], to_image=to["image"])
    for t in transfer_rows:
        t["image"] = m(chain, t["token"]).get("image")
        t["symbol"] = t.get("symbol") or m(chain, t["token"])["symbol"]

    act = summary["activity"]
    return {
        "address": address,
        "chain": chain,
        "kind": w.address_kind(address),
        "capabilities": caps,
        "status": {**raw["status"], "transfers": tr_status},
        "profile": summary,
        "first_seen_ts": act.get("first_seen_ts"),
        "last_seen_ts": act.get("last_seen_ts"),
        "holdings": holdings,
        "performance": performance,
        "trades": trades,
        "transfers": transfer_rows,
        "by_chain": _by_chain(summary["pnl_features"], raw["balances"]),
        "credits_used": client.credits_used,
    }
