"""Token X-ray: who holds and trades a token, labeled by core.wallets, with a rule-based verdict.

1. token_context(): the header card (price, liquidity, FDV, GT Score, honeypot, authorities,
   dev holding, holder distribution, launchpad) from the token + token info endpoints.
2. run(): top holders + top traders (1 call each), labeled instantly from their rows, then the top
   wallets are enriched with their own lifetime PnL (1 call each, cached) so "proven trader" means
   proven across every token they've traded, not just this one. Streams as SSE.
3. The verdict is core.wallets.xray_verdict(): the first matching rule wins and is shown to the user.
"""
import asyncio
import time

from core import wallets as w
from core.client import CoinGeckoClient, CoinGeckoError

from . import chains, profile

HOLDERS = 40
TRADERS = 40
ENRICH_MAX = 30


def _change(pool_attrs: dict, key: str):
    return w._f((pool_attrs.get("price_change_percentage") or {}).get(key))


async def token_context(client: CoinGeckoClient, chain: str, token: str, pool: str | None = None) -> dict:
    """Header-card data for a token. Raises CoinGeckoError if the token can't be found on `chain`."""
    doc = await client.token(chain, token)
    a = doc["attributes"]
    pools = doc["pools"]
    by_liquidity = sorted(pools, key=lambda p: w._f((p.get("attributes") or {}).get("reserve_in_usd"), 0.0) or 0.0, reverse=True)
    chosen = next((p for p in pools if (p.get("attributes") or {}).get("address", "").lower() == (pool or "").lower()), by_liquidity[0] if by_liquidity else None)
    pa = (chosen or {}).get("attributes") or {}
    try:
        info = await client.token_info(chain, token)
    except CoinGeckoError:
        info = {}
    holders = info.get("holders") or {}
    image = a.get("image_url") if a.get("image_url") not in (None, "missing.png") else None
    price_history = []
    pool_addr = pa.get("address")
    if pool_addr:
        try:
            candles = await client.pool_ohlcv(chain, pool_addr, "hour", aggregate=1, limit=48)
            price_history = [{"ts": row[0], "close": row[4]} for row in reversed(candles) if len(row) >= 5]
            if len(price_history) < 5:
                # A thinly-traded pool can have almost no swaps in the last 48 hours even though
                # the token itself has a long history — widen to daily candles over ~6 months
                # rather than showing an empty chart for an established token.
                daily = await client.pool_ohlcv(chain, pool_addr, "day", aggregate=1, limit=180)
                daily_history = [{"ts": row[0], "close": row[4]} for row in reversed(daily) if len(row) >= 5]
                if len(daily_history) > len(price_history):
                    price_history = daily_history
        except CoinGeckoError:
            price_history = []
    return {
        "chain": chain,
        "chain_label": chains.label(chain),
        "address": a.get("address") or token,
        "name": a.get("name"),
        "symbol": a.get("symbol"),
        "image_url": image,
        "price_usd": w._f(a.get("price_usd")),
        "fdv_usd": w._f(a.get("fdv_usd")),
        "market_cap_usd": w._f(a.get("market_cap_usd")),
        "volume_24h_usd": w._f((a.get("volume_usd") or {}).get("h24")),
        "liquidity_usd": w._f(a.get("total_reserve_in_usd")),
        "change_1h": _change(pa, "h1"),
        "change_6h": _change(pa, "h6"),
        "change_24h": _change(pa, "h24"),
        "pool_address": pa.get("address"),
        "pool_name": pa.get("name"),
        "pool_created_at": pa.get("pool_created_at"),
        "price_history": price_history,
        "gt_score": w._f(info.get("gt_score")),
        "gt_score_details": info.get("gt_score_details") or {},
        "gt_verified": info.get("gt_verified"),
        "is_honeypot": info.get("is_honeypot"),
        "mint_authority": info.get("mint_authority"),
        "freeze_authority": info.get("freeze_authority"),
        "developer_holding_pct": w._f(info.get("developer_holding_percentage")),
        "holders_count": holders.get("count"),
        "holder_distribution": {k: w._f(v) for k, v in (holders.get("distribution_percentage") or {}).items()},
        "launchpad": info.get("launchpad_details") or a.get("launchpad_details"),
        "categories": info.get("categories") or [],
        "twitter": info.get("twitter_handle"),
        "websites": info.get("websites") or [],
        "info_available": bool(info),
        "_info": info,
    }


async def wallet_rows(client: CoinGeckoClient, chain: str, token: str) -> tuple[list[dict], dict]:
    """Top holders + top traders, merged by address. Returns (rows, {holders, traders} status)."""
    status = {"holders": "ok", "traders": "ok"}
    holders, traders = [], []
    try:
        holders = await client.get(f"/onchain/networks/{chain}/tokens/{token}/top_holders", {"holders": HOLDERS, "include_pnl_details": "true"}, ttl=120)
        holders = ((holders.get("data") or {}).get("attributes") or {}).get("holders") or []
    except CoinGeckoError as e:
        status["holders"] = "unavailable" if e.status in (400, 404, 422) else "error"
    try:
        traders = await client.top_traders(chain, token, n=TRADERS)
    except CoinGeckoError as e:
        status["traders"] = "unavailable" if e.status in (400, 404, 422) else "error"
    rows: dict[str, dict] = {}
    for h in holders:
        if h.get("address"):
            rows[h["address"].lower()] = {**h, "_source": "holder"}
    for t in traders:
        if not t.get("address"):
            continue
        k = t["address"].lower()
        if k in rows:
            rows[k] = {**t, **{kk: vv for kk, vv in rows[k].items() if vv is not None}, "_source": "holder + trader"}
        else:
            rows[k] = {**t, "_source": "trader"}
    return list(rows.values()), status


def label_row(row: dict, pnl_feat: dict | None = None, budget_usd: float = 100) -> dict:
    """One X-ray wallet: normalized token row + label, stance, and (when enriched) lifetime stats."""
    t = w.token_row(row)
    label = w.xray_label(t, pnl_feat)
    out = {
        "address": t["address"],
        "role": t["role"],
        "api_label": t["api_label"],
        "label": label,
        "label_rule": w.LABEL_RULES.get(label, ""),
        "stance": w.holder_stance(t),
        "token": t,
        "enriched": pnl_feat is not None,
    }
    if pnl_feat:
        out["lifetime"] = pnl_feat
        out["skill_score"] = w.skill_score({"trades": 0}, pnl_feat)
        out["copyability"] = w.copyability(pnl_feat.get("avg_trade_usd"), None, budget_usd)
        out["copyable"] = label == "proven_trader" and w.is_copyable(out["copyability"])
    return out


async def run(client: CoinGeckoClient, chain: str, token: str, budget_usd: float = 100, ctx: dict | None = None):
    """SSE generator: start (rows labeled from the token data alone) -> wallet (enriched) -> verdict -> done."""
    t0 = time.perf_counter()
    credits0 = client.credits_used
    rows, status = await wallet_rows(client, chain, token)
    wallets = [label_row(r, None, budget_usd) for r in rows]
    yield "start", {"total": len(wallets), "status": status, "wallets": wallets}
    if not wallets:
        yield "verdict", {"facts": w.xray_facts([]), "composition": {}, "verdict": None}
        yield "done", {"elapsed_ms": round((time.perf_counter() - t0) * 1000), "credits": client.credits_used - credits0}
        return

    def weight(x: dict) -> float:
        t = x["token"]
        return (t.get("supply_pct") or 0) * 10_000 + (t.get("bought_usd") or 0) + (t.get("sold_usd") or 0)

    targets = [x for x in sorted(wallets, key=lambda x: -weight(x)) if x["label"] != "protocol" and w.address_kind(x["address"])][:ENRICH_MAX]
    by_addr = {x["address"].lower(): r for x, r in zip(wallets, rows)}
    index = {x["address"].lower(): i for i, x in enumerate(wallets)}
    queue: asyncio.Queue = asyncio.Queue()

    async def enrich(x: dict):
        nets = profile.wallet_networks(x["address"], chain)
        try:
            pnl = await client.wallet_pnl(x["address"], nets, per_page=100, sort="total_buy_usd_desc")
            feat = w.pnl_features(pnl)
        except CoinGeckoError:
            feat = None
        await queue.put(label_row(by_addr[x["address"].lower()], feat, budget_usd) if feat else {**x, "enriched": True})

    tasks = [asyncio.create_task(enrich(x)) for x in targets]
    try:
        for done in range(1, len(targets) + 1):
            row = await queue.get()
            wallets[index[row["address"].lower()]] = row
            yield "wallet", row
            yield "progress", {"done": done, "total": len(targets), "elapsed_ms": round((time.perf_counter() - t0) * 1000), "credits": client.credits_used - credits0}
    finally:
        for t in tasks:
            t.cancel()
    facts = w.xray_facts(wallets)
    info = (ctx or {}).get("_info") or {}
    yield "verdict", {"facts": facts, "composition": w.composition(wallets), "verdict": w.xray_verdict(facts, info)}
    yield "done", {"elapsed_ms": round((time.perf_counter() - t0) * 1000), "credits": client.credits_used - credits0}


def public_context(ctx: dict) -> dict:
    """The context minus internal fields, for JSON responses."""
    return {k: v for k, v in ctx.items() if not k.startswith("_")}

