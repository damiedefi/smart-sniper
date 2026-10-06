"""Deterministic wallet profiling shared by every starter: FIFO PnL, bot heuristics, labels, copyability."""
import re
import statistics
from collections import defaultdict, deque
from datetime import datetime, timezone

STABLES = {"USDC", "USDT", "DAI", "USDG", "USDS", "USDE", "PYUSD", "FDUSD", "TUSD", "USD1", "USDC.E", "USDBC", "LUSD", "FRAX", "GHO", "RLUSD"}

# A wallet's lifetime realized or unrealized PnL past this is treated as unreliable, not displayed as
# a number. Broken/rug tokens can report an arbitrarily large fake price; a real wallet's PnL doesn't
# reach nine or more figures from genuine trading. Generous on purpose, so no real trader gets capped.
PNL_SANITY_CEILING_USD = 25_000_000


def _f(x, default=None):
    """float(x), or default if x is missing or not a number."""
    if x is None or x == "":
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _ts(value) -> float | None:
    """A unix timestamp or an ISO8601 string, normalized to unix seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ---- FIFO matching over a wallet's own trade history ----


def normalize_trades(rows: list[dict]) -> list[dict]:
    """Onchain wallet-trades API rows -> {token, kind, qty, usd, ts}, oldest first."""
    out = []
    for r in rows:
        kind = r.get("kind")
        if kind == "buy":
            token, qty = r.get("to_token_address"), _f(r.get("to_token_amount"), 0.0)
        elif kind == "sell":
            token, qty = r.get("from_token_address"), _f(r.get("from_token_amount"), 0.0)
        else:
            continue
        ts = _ts(r.get("block_timestamp"))
        if ts is None or not qty:
            continue
        out.append({"token": token, "kind": kind, "qty": qty, "usd": _f(r.get("volume_in_usd"), 0.0) or 0.0, "ts": ts})
    return sorted(out, key=lambda t: t["ts"])


def fifo_matches(trades: list[dict]) -> list[dict]:
    """Matches each sell against the earliest unmatched buy quantity of the same token."""
    lots: dict = defaultdict(deque)
    closed = []
    for t in trades:
        if t["kind"] == "buy":
            price = t["usd"] / t["qty"] if t["qty"] else 0.0
            lots[t["token"]].append({"qty": t["qty"], "price": price, "ts": t["ts"]})
            continue
        remaining = t["qty"]
        sell_price = t["usd"] / t["qty"] if t["qty"] else 0.0
        while remaining > 1e-12 and lots[t["token"]]:
            lot = lots[t["token"]][0]
            matched = min(lot["qty"], remaining)
            closed.append(
                {
                    "token": t["token"],
                    "qty": matched,
                    "buy_usd": matched * lot["price"],
                    "sell_usd": matched * sell_price,
                    "pnl_usd": matched * (sell_price - lot["price"]),
                    "hold_s": max(t["ts"] - lot["ts"], 0),
                    "opened_ts": lot["ts"],
                    "closed_ts": t["ts"],
                }
            )
            lot["qty"] -= matched
            remaining -= matched
            if lot["qty"] <= 1e-12:
                lots[t["token"]].popleft()
    return closed


def match_metrics(closed: list[dict]) -> dict:
    """Win rate, avg hold, median trade USD, and realized PnL from a wallet's own FIFO-matched trades."""
    if not closed:
        return {"trades": 0, "win_rate": None, "avg_hold_s": None, "median_trade_usd": None, "realized_pnl_usd": 0.0}
    wins = sum(1 for c in closed if c["pnl_usd"] > 0)
    return {
        "trades": len(closed),
        "win_rate": round(wins / len(closed), 3),
        "avg_hold_s": round(statistics.mean(c["hold_s"] for c in closed), 0),
        "median_trade_usd": round(statistics.median(c["sell_usd"] for c in closed), 2),
        "realized_pnl_usd": round(sum(c["pnl_usd"] for c in closed), 2),
    }


# ---- lifetime PnL and recent-activity summaries, from the wallet endpoints ----


def _sane_lifetime_pnl(stats: list[dict], field: str) -> tuple[float, int]:
    """Sums a per-token PnL field, excluding positions whose PnL is wildly inconsistent with their own
    traded volume (a strong sign of a mispriced/broken token, e.g. a fake token with a manipulated
    price). Returns (sum, count_excluded). A single garbage token can otherwise make a wallet's whole
    lifetime PnL meaningless (seen live: one token inflated a wallet's unrealized PnL past $2 septillion).
    """
    total, excluded = 0.0, 0
    for s in stats:
        value = _f(s.get(field), 0.0) or 0.0
        volume = (_f(s.get("total_buy_usd"), 0.0) or 0.0) + (_f(s.get("total_sell_usd"), 0.0) or 0.0)
        if abs(value) > max(volume * 25, 2_000_000) and abs(value) > 250_000:
            excluded += 1
            continue
        total += value
    return total, excluded


def pnl_features(attrs: dict | None) -> dict:
    """Summarizes GET /onchain/wallets/{address}/pnl into lifetime win rate, concentration, volume and per-chain PnL."""
    if not attrs:
        return {"available": False}
    stats = attrs.get("token_stats") or []
    sold = [_f(s.get("realized_pnl_usd"), 0.0) for s in stats if (s.get("total_sell_count") or 0) > 0]
    wins = [r for r in sold if r > 0]
    positive = [_f(s.get("realized_pnl_usd"), 0.0) for s in stats if _f(s.get("realized_pnl_usd"), 0.0) > 0]
    buys = sum(s.get("total_buy_count") or 0 for s in stats)
    sells = sum(s.get("total_sell_count") or 0 for s in stats)
    volume = sum((_f(s.get("total_buy_usd"), 0.0) or 0) + (_f(s.get("total_sell_usd"), 0.0) or 0) for s in stats)
    nets = [n for n in (attrs.get("networks") or []) if (n.get("tokens") or 0) > 0]
    realized_clean, realized_excl = _sane_lifetime_pnl(stats, "realized_pnl_usd")
    unrealized_clean, unrealized_excl = _sane_lifetime_pnl(stats, "unrealized_pnl_usd")
    raw_realized = round(_f(attrs.get("total_realized_pnl_usd"), 0.0), 0)
    raw_unrealized = round(_f(attrs.get("total_unrealized_pnl_usd"), 0.0), 0)
    outliers_excluded = realized_excl + unrealized_excl
    # The API's own lifetime total can include tokens beyond this 200-row page, so a bad position we
    # never even see can still blow up the headline number. If the API's total disagrees wildly with
    # what we can actually verify from the visible (and already-filtered) positions, trust the visible
    # sum instead — it's sorted by size, so it already captures the wallet's real, meaningful PnL.
    def _disagrees(raw: float, clean: float) -> bool:
        return abs(raw) > max(abs(clean) * 10, 1_000_000) and abs(raw - clean) > 1_000_000
    use_clean = outliers_excluded > 0 or _disagrees(raw_realized, realized_clean) or _disagrees(raw_unrealized, unrealized_clean)
    realized_best = realized_clean if use_clean else raw_realized
    unrealized_best = unrealized_clean if use_clean else raw_unrealized
    # Final backstop: even the "cleaned" number can still be absurd if several borderline positions
    # each individually dodged the per-token filter above. Nothing a wallet actually trades is worth
    # more than this in practice; past it we show "unreliable" rather than any number at all, since a
    # wrong-but-plausible-looking correction is worse than an honest gap.
    reliable = abs(realized_best) <= PNL_SANITY_CEILING_USD and abs(unrealized_best) <= PNL_SANITY_CEILING_USD
    return {
        "available": True,
        "tokens_traded": attrs.get("total_tokens"),
        "tokens_in_sample": len(stats),
        "tokens_sold": len(sold),
        "lifetime_realized_pnl_usd": round(realized_best, 0) if reliable else None,
        "lifetime_unrealized_pnl_usd": round(unrealized_best, 0) if reliable else None,
        "lifetime_realized_pnl_usd_raw": raw_realized,
        "lifetime_unrealized_pnl_usd_raw": raw_unrealized,
        "pnl_reliable": reliable,
        "pnl_outliers_excluded": outliers_excluded,
        "win_rate_tokens": round(len(wins) / len(sold), 3) if sold else None,
        "profit_concentration": round(max(positive) / sum(positive), 3) if positive else None,
        "total_buys": buys,
        "total_sells": sells,
        "volume_usd": round(volume, 0),
        "avg_trade_usd": round(volume / (buys + sells), 0) if buys + sells else None,
        "active_networks": [n.get("network") for n in sorted(nets, key=lambda n: -(n.get("tokens") or 0))],
        "pnl_by_network": {
            n.get("network"): {
                "realized_pnl_usd": round(_f(n.get("realized_pnl_usd"), 0.0), 0),
                "unrealized_pnl_usd": round(_f(n.get("unrealized_pnl_usd"), 0.0), 0),
                "tokens": n.get("tokens") or 0,
            }
            for n in nets
        },
    }


def portfolio_features(attrs: dict | None) -> dict:
    """Summarizes GET /onchain/wallets/{address}/balances into value, concentration and stablecoin share."""
    if not attrs:
        return {"available": False}
    items = attrs.get("balances") or []
    values = sorted(((_f(i.get("value_usd"), 0.0) or 0) for i in items), reverse=True)
    total = _f(attrs.get("total_value_usd"), 0.0) or sum(values)
    stable = sum((_f(i.get("value_usd"), 0.0) or 0) for i in items if (i.get("symbol") or "").upper() in STABLES)
    return {
        "available": True,
        "portfolio_value_usd": round(total, 0),
        "holdings": attrs.get("total_holdings") or len(items),
        "networks_with_balance": [n.get("network") for n in (attrs.get("networks") or []) if (_f(n.get("value_usd"), 0.0) or 0) >= 1],
        "top_holding_share": round(values[0] / total, 3) if total and values else None,
        "stablecoin_share": round(stable / total, 3) if total else None,
    }


def activity_features(trades: list[dict], now: float | None = None) -> dict:
    """Trading cadence from a wallet's most recent trades: per-day rate, buy share, gaps, days since last trade."""
    now = now or datetime.now(timezone.utc).timestamp()
    stamps = sorted(t for t in (_ts(x.get("block_timestamp")) for x in trades) if t)
    if not stamps:
        return {"recent_trades": 0, "recent_trades_per_day": 0, "days_since_last_trade": None, "trades_by_day": [0] * 7}
    span_days = max((stamps[-1] - stamps[0]) / 86400, 1 / 24)
    buys = sum(1 for x in trades if x.get("kind") == "buy")
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    by_day = [0] * 7  # oldest -> newest, last 7 x 24h windows ending now
    for t in stamps:
        age_days = int((now - t) // 86400)
        if 0 <= age_days < 7:
            by_day[6 - age_days] += 1
    return {
        "recent_trades": len(stamps),
        "recent_trades_per_day": round(len(stamps) / span_days, 1),
        "recent_buy_share": round(buys / len(trades), 2) if trades else None,
        "median_seconds_between_trades": round(statistics.median(gaps), 0) if gaps else None,
        "distinct_pools": len({x.get("pool_address") for x in trades if x.get("pool_address")}),
        "days_since_last_trade": round((now - stamps[-1]) / 86400, 1),
        "first_seen_ts": stamps[0],
        "last_seen_ts": stamps[-1],
        "trades_by_day": by_day,
    }


def likely_bot_row(row: dict) -> bool:
    """Cheap pre-filter for candidate lists: thousands of trades in a single token."""
    return (row.get("total_buy_count") or 0) + (row.get("total_sell_count") or 0) > 1500


# ---- infrastructure / contract detection ----
#
# CoinGecko's wallet endpoints answer for any address, including contracts: routers, AMM pools,
# token-approval helpers, account-abstraction singletons, treasuries. Those aren't discretionary
# traders, and their PnL/activity numbers don't mean what they'd mean for a person. We can't check
# on-chain bytecode from this API, so this is two honest, separate signals:
#  1. a short list of addresses that are universally known to be infrastructure, matched exactly
#  2. a heuristic for everything else: enormous trade volume spread over many chains at the same
#     address, which real individually-managed wallets essentially never show
#
# Neither is a certainty. Both are surfaced as "likely", not "confirmed".

KNOWN_INFRA_ADDRESSES: dict[str, str] = {
    # Same address on (almost) every EVM chain via a CREATE2 factory.
    "0x000000000022d473030f116ddee9f6b43ac78ba3": "Permit2 (Uniswap token-approval helper)",
    "0xca11bde05977b3631167028862be2a173976ca11": "Multicall3",
    "0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789": "ERC-4337 EntryPoint v0.6",
    "0x0000000071727de22e5e9d8baf0edac6f37da032": "ERC-4337 EntryPoint v0.7",
    "0x66a9893cc07d91d95644aedd05d03f95e1dba8af": "Uniswap Universal Router",
    "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch Aggregation Router",
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff": "0x Exchange Proxy",
    "0x9008d19f58aabd9ed0d60971565aa8510560ab41": "CoW Protocol Settlement",
    # Canonical wrapped-native predeploys/tokens (per chain, so not one universal address).
    "0x4200000000000000000000000000000000000006": "Wrapped ETH predeploy (OP-stack chains)",
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "WETH (Ethereum)",
}


def known_infra(address: str) -> str | None:
    """The name of a well-known infrastructure contract at this address, if it's an exact match."""
    return KNOWN_INFRA_ADDRESSES.get((address or "").lower())


def looks_like_infra_contract(pnl: dict, activity: dict) -> bool:
    """Heuristic: huge trade volume spread across many chains at once. Real wallets rarely look like
    this; shared infrastructure (routers, pools, singleton helpers) commonly does."""
    total_trades = (pnl.get("total_buys") or 0) + (pnl.get("total_sells") or 0)
    chains = len(pnl.get("active_networks") or [])
    tokens = pnl.get("tokens_traded") or 0
    return (total_trades > 3000 and chains >= 4) or (tokens > 300 and chains >= 3)


# ---- labels + copyability ----


def label_wallet(metrics: dict, pnl: dict, activity: dict, address: str | None = None) -> str:
    """One rule-based label, checked in priority order: protocol, bot_like, dormant, proven_trader,
    one_hit, whale, flipper. `protocol` covers both known infra addresses and the high-volume,
    many-chain heuristic (see looks_like_infra_contract); it's the only label that can be wrong in
    either direction, so it's shown with a plain "likely" / named-match caveat, never as a hard fact.
    """
    if address and known_infra(address):
        return "protocol"
    if looks_like_infra_contract(pnl, activity):
        return "protocol"
    if (activity.get("recent_trades_per_day") or 0) > 50:
        return "bot_like"
    days_idle = activity.get("days_since_last_trade")
    if metrics["trades"] == 0:
        return "dormant" if (days_idle or 0) > 14 else "one_hit"
    if metrics["trades"] >= 5 and (metrics["win_rate"] or 0) >= 0.6 and metrics["realized_pnl_usd"] > 0:
        return "proven_trader"
    if metrics["trades"] == 1:
        return "one_hit"
    if days_idle and days_idle > 14:
        return "dormant"
    if (pnl.get("lifetime_realized_pnl_usd") or 0) > 100_000:
        return "whale"
    if (activity.get("recent_trades_per_day") or 0) > 10:
        return "flipper"
    return "flipper"


def copyability(median_trade_usd: float | None, trades_per_day: float | None, budget_usd: float) -> int:
    """0-100: can a budget_usd account actually mirror this wallet's typical trade size and pace?"""
    if not median_trade_usd or budget_usd <= 0:
        return 0
    size_ratio = budget_usd / median_trade_usd
    size_score = 100 if size_ratio >= 1 else max(0, round(size_ratio * 100))
    freq = trades_per_day or 0
    freq_score = 100 if freq <= 5 else max(0, round(100 - (freq - 5) * 5))
    return round(size_score * 0.7 + freq_score * 0.3)


def is_copyable(copy_score: int | None, threshold: int = 60) -> bool:
    """The 'copyable at my budget' filter: copyability at or above `threshold`."""
    return (copy_score or 0) >= threshold


def skill_score(match_metrics: dict, pnl_features: dict) -> int:
    """0-100: win rate (50%), lifetime realized PnL capped at $50k (30%), sample size capped at 20 trades (20%).

    Win rate and sample come from the wallet's own FIFO-matched trades when it has any, and fall back to
    the lifetime per-token win rate from the PnL endpoint when the recent trade window has no round trips.
    """
    trades = match_metrics.get("trades") or 0
    win_rate = match_metrics.get("win_rate") if trades else pnl_features.get("win_rate_tokens")
    sample = max(trades, pnl_features.get("tokens_sold") or 0)
    lifetime_pnl = max(pnl_features.get("lifetime_realized_pnl_usd") or 0, 0)
    pnl_component = min(lifetime_pnl, 50_000) / 50_000
    sample_component = min(sample, 20) / 20
    return round(min(100, max(0, (win_rate or 0) * 50 + pnl_component * 30 + sample_component * 20)))


# ---- trading style + tags ----

STYLES = ("bot", "scalper", "swing", "holder")


def trading_style(match_metrics: dict, activity: dict) -> str | None:
    """How a wallet trades, from hold time and cadence: bot, scalper (<1h holds), swing (<7d) or holder."""
    per_day = activity.get("recent_trades_per_day") or 0
    gap = activity.get("median_seconds_between_trades")
    if per_day > 50 or (gap is not None and gap <= 10 and (activity.get("recent_trades") or 0) >= 20):
        return "bot"
    hold = match_metrics.get("avg_hold_s")
    if hold is None:
        if (activity.get("recent_buy_share") or 0) >= 0.8:
            return "holder"
        return None
    if hold < 3600:
        return "scalper"
    if hold < 7 * 86400:
        return "swing"
    return "holder"


def wallet_tags(pnl: dict, portfolio: dict, activity: dict, match_metrics: dict) -> list[str]:
    """Short, rule-based descriptors shown as chips. Each rule is one line so it's easy to add your own."""
    tags = []
    if len(pnl.get("active_networks") or []) >= 3:
        tags.append("multi_chain")
    if (portfolio.get("portfolio_value_usd") or 0) >= 1_000_000 or (pnl.get("avg_trade_usd") or 0) >= 50_000:
        tags.append("whale_sized")
    if (portfolio.get("stablecoin_share") or 0) >= 0.5 and (portfolio.get("portfolio_value_usd") or 0) >= 1000:
        tags.append("stablecoin_heavy")
    if (activity.get("recent_trades_per_day") or 0) >= 20:
        tags.append("high_frequency")
    if (match_metrics.get("trades") or 0) >= 5 and (match_metrics.get("win_rate") or 0) >= 0.6:
        tags.append("disciplined_exits")
    if (pnl.get("profit_concentration") or 0) >= 0.7 and (pnl.get("tokens_sold") or 0) >= 3:
        tags.append("one_big_win")
    if (match_metrics.get("avg_hold_s") or 0) >= 7 * 86400:
        tags.append("diamond_hands")
    days = activity.get("days_since_last_trade")
    if days is not None and days < 1:
        tags.append("active_today")
    if days is None or days > 30:
        tags.append("dormant")
    if pnl.get("available") and (pnl.get("tokens_traded") or 0) <= 3:
        tags.append("fresh")
    return tags


# ---- addresses ----

_EVM_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BASE58_ADDR = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def address_kind(address: str | None) -> str | None:
    """'evm' for 0x + 40 hex, 'solana' for base58 (32-44 chars), else None."""
    a = (address or "").strip()
    if _EVM_ADDR.match(a):
        return "evm"
    if _BASE58_ADDR.match(a):
        return "solana"
    return None


def primary_network(pnl_attrs: dict | None, fallback: str) -> str:
    """The network a wallet has traded the most tokens on, from the PnL endpoint's per-network rows."""
    nets = [n for n in ((pnl_attrs or {}).get("networks") or []) if (n.get("tokens") or 0) > 0]
    if not nets:
        return fallback
    return max(nets, key=lambda n: n.get("tokens") or 0)["network"]


# ---- Token X-ray: labels for a token's top holders/traders, composition, verdict ----

LABEL_RULES = {
    "proven_trader": "5+ closed trades with a 60%+ win rate and positive realized PnL",
    "whale": "position or lifetime PnL above $100k, or 2%+ of supply",
    "bot_like": "1,500+ trades in one token, or 50+ trades a day",
    "flipper": "trades often, in and out fast",
    "one_hit": "only one closed trade so far",
    "dormant": "no trades in over two weeks",
    "insider_like": "holds 0.5%+ of supply but never bought it",
    "fresh_wallet": "has traded 3 tokens or fewer",
    "protocol": "a named contract, pool or exchange wallet with no trades",
    "holder": "holds the token, not enough history to say more",
}


def token_row(row: dict) -> dict:
    """A top_holders / top_traders row, normalized to the fields the X-ray needs."""
    buys = row.get("total_buy_count")
    sells = row.get("total_sell_count")
    return {
        "address": row.get("address"),
        "api_label": row.get("label") or row.get("name"),
        "role": row.get("_source") or ("holder" if row.get("percentage") is not None else "trader"),
        "supply_pct": _f(row.get("percentage")),
        "value_usd": _f(row.get("value")),
        "balance": _f(row.get("amount")) if row.get("amount") is not None else _f(row.get("token_balance")),
        "realized_pnl_usd": _f(row.get("realized_pnl_usd")),
        "unrealized_pnl_usd": _f(row.get("unrealized_pnl_usd")),
        "buy_count": buys,
        "sell_count": sells,
        "bought_usd": _f(row.get("total_buy_usd"), 0.0) or 0.0,
        "sold_usd": _f(row.get("total_sell_usd"), 0.0) or 0.0,
        "avg_buy_usd": _f(row.get("average_buy_price_usd")),
        "avg_sell_usd": _f(row.get("average_sell_price_usd")),
    }


def holder_stance(t: dict) -> str | None:
    """What a wallet is doing in this token: accumulating, holding, distributing or exited."""
    buys, sells = t.get("buy_count"), t.get("sell_count")
    held = (t.get("supply_pct") or 0) > 0 or (t.get("balance") or 0) > 0
    if buys is None and sells is None:
        return "holding" if held else None
    buys, sells = buys or 0, sells or 0
    if sells and not held:
        return "exited"
    if buys > sells * 1.5:
        return "accumulating"
    if sells > buys:
        return "distributing"
    return "holding"


def xray_label(t: dict, pnl: dict | None = None) -> str:
    """One label for a wallet in a token's X-ray, checked in priority order. `pnl` is optional pnl_features()."""
    pnl = pnl or {}
    trades_here = (t.get("buy_count") or 0) + (t.get("sell_count") or 0)
    lifetime_trades = (pnl.get("total_buys") or 0) + (pnl.get("total_sells") or 0)
    if t.get("api_label") and not trades_here and not pnl.get("tokens_traded"):
        return "protocol"
    if trades_here > 1500 or (pnl.get("tokens_in_sample") and lifetime_trades / max(pnl["tokens_in_sample"], 1) > 1500):
        return "bot_like"
    if "holder" in (t.get("role") or "") and not (t.get("buy_count") or 0) and (t.get("supply_pct") or 0) >= 0.5:
        return "insider_like"
    if (pnl.get("tokens_sold") or 0) >= 5 and (pnl.get("win_rate_tokens") or 0) >= 0.6 and (pnl.get("lifetime_realized_pnl_usd") or 0) > 0:
        return "proven_trader"
    big = max(t.get("value_usd") or 0, abs(pnl.get("lifetime_realized_pnl_usd") or 0), (t.get("realized_pnl_usd") or 0))
    if big >= 100_000 or (t.get("supply_pct") or 0) >= 2:
        return "whale"
    if pnl.get("available") and (pnl.get("tokens_traded") or 0) <= 3:
        return "fresh_wallet"
    if (t.get("buy_count") or 0) >= 3 and (t.get("sell_count") or 0) >= 3:
        return "flipper"
    if (t.get("sell_count") or 0) == 1 and (t.get("buy_count") or 0) <= 1:
        return "one_hit"
    return "holder"


def composition(wallets: list[dict]) -> dict:
    """Share of the X-rayed wallets per label, three ways: by wallet count, traded volume, and supply held."""
    comp: dict[str, dict] = {}
    for w in wallets:
        t = w.get("token") or {}
        c = comp.setdefault(w.get("label", "holder"), {"wallets": 0, "volume_usd": 0.0, "supply_pct": 0.0})
        c["wallets"] += 1
        c["volume_usd"] += (t.get("bought_usd") or 0) + (t.get("sold_usd") or 0)
        c["supply_pct"] += t.get("supply_pct") or 0
    for c in comp.values():
        c["volume_usd"] = round(c["volume_usd"], 0)
        c["supply_pct"] = round(c["supply_pct"], 2)
    return comp


def xray_facts(wallets: list[dict]) -> dict:
    """The numbers the verdict rules read: smart-money net flow, bot volume share, insider/whale supply."""
    vol_total = sum((w["token"].get("bought_usd") or 0) + (w["token"].get("sold_usd") or 0) for w in wallets)
    smart = [w for w in wallets if w.get("label") == "proven_trader"]
    bots = [w for w in wallets if w.get("label") == "bot_like"]
    bot_vol = sum((w["token"].get("bought_usd") or 0) + (w["token"].get("sold_usd") or 0) for w in bots)
    return {
        "wallets": len(wallets),
        "smart_wallets": len(smart),
        "smart_net_flow_usd": round(sum((w["token"].get("bought_usd") or 0) - (w["token"].get("sold_usd") or 0) for w in smart), 0),
        "smart_accumulating": sum(1 for w in smart if w.get("stance") == "accumulating"),
        "bot_volume_share": round(bot_vol / vol_total, 3) if vol_total else 0.0,
        "insider_supply_pct": round(sum(w["token"].get("supply_pct") or 0 for w in wallets if w.get("label") == "insider_like"), 2),
        "whale_supply_pct": round(sum(w["token"].get("supply_pct") or 0 for w in wallets if w.get("label") == "whale"), 2),
        "copyable_wallets": sum(1 for w in wallets if w.get("copyable")),
    }


VERDICTS = {
    "honeypot": ("Honeypot flagged", "danger"),
    "bot_driven": ("Bot-driven", "warning"),
    "insider_heavy": ("Insider-heavy", "danger"),
    "smart_accumulating": ("Smart money accumulating", "success"),
    "smart_distributing": ("Smart money distributing", "danger"),
    "whale_heavy": ("Whale-heavy", "info"),
    "organic": ("Organic / mixed", "neutral"),
}


def xray_verdict(facts: dict, info: dict | None = None) -> dict:
    """One verdict for a token, first matching rule wins. The rule that fired is returned with it."""
    info = info or {}
    top10 = _f(((info.get("holders") or {}).get("distribution_percentage") or {}).get("top_10"))

    def out(key: str, rule: str) -> dict:
        label, tone = VERDICTS[key]
        return {"key": key, "label": label, "tone": tone, "rule": rule}

    if str(info.get("is_honeypot")).lower() in ("yes", "true"):
        return out("honeypot", "the token info endpoint flags it as a honeypot")
    if facts.get("bot_volume_share", 0) >= 0.5:
        return out("bot_driven", f"likely bots made {facts['bot_volume_share'] * 100:.0f}% of the top wallets' traded volume (rule: 50%+)")
    if facts.get("insider_supply_pct", 0) >= 15:
        return out("insider_heavy", f"wallets that never bought hold {facts['insider_supply_pct']:.1f}% of supply (rule: 15%+)")
    if facts.get("smart_wallets", 0) >= 3 and facts.get("smart_net_flow_usd", 0) > 0:
        return out("smart_accumulating", f"{facts['smart_wallets']} proven traders are net buyers (+${facts['smart_net_flow_usd']:,.0f} bought minus sold)")
    if facts.get("smart_wallets", 0) >= 3 and facts.get("smart_net_flow_usd", 0) < 0:
        return out("smart_distributing", f"{facts['smart_wallets']} proven traders are net sellers (-${abs(facts['smart_net_flow_usd']):,.0f} sold minus bought)")
    if facts.get("whale_supply_pct", 0) >= 30 or (top10 or 0) >= 50:
        which = f"top 10 holders own {top10:.0f}%" if (top10 or 0) >= 50 else f"whales hold {facts['whale_supply_pct']:.0f}% of supply"
        return out("whale_heavy", f"{which} (rule: 50%+ top-10 or 30%+ whale supply)")
    return out("organic", "no single group dominates volume or supply")
