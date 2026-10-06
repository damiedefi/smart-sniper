"""Follow / forward test: poll followed wallets' trades, mirror them into the paper engine.

Copies are detected by polling, not streaming (there's no wallet-level WebSocket channel), so this
is honest about latency: a followed wallet's trade shows up here up to `poll_s` seconds late, and
every decision records its detection lag (seconds between the onchain trade and us seeing it).

Followed wallets can be on different chains: pass addresses as plain strings (all on `chain`) or as
{"address", "chain"} dicts. Open paper positions are marked to market every poll with the onchain
simple-price endpoint, so the equity curve moves even when nobody trades.
"""
import time

from core import wallets as w
from core.client import CoinGeckoClient, CoinGeckoError
from core.paper import Portfolio

from . import tokens

FEED_MAX = 200


def _targets(addresses: list, chain: str) -> list[dict]:
    out, seen = [], set()
    for a in addresses or []:
        t = {"address": a, "chain": chain} if isinstance(a, str) else {"address": a.get("address"), "chain": a.get("chain") or chain}
        if t["address"] and t["address"].lower() not in seen:
            seen.add(t["address"].lower())
            out.append(t)
    return out


class FollowEngine:
    """Mirrors buys/sells from a list of followed wallets into one paper Portfolio, scaled to budget."""

    def __init__(self, client: CoinGeckoClient, chain: str, addresses: list, budget_usd: float, assumptions: dict, poll_s: float = 30, label: str | None = None):
        self.client = client
        self.chain = chain
        self.targets = _targets(addresses, chain)
        self.poll_s = poll_s
        self.budget_usd = budget_usd
        self.assumptions = assumptions
        self.label = label
        self.portfolio = Portfolio(
            cash=budget_usd,
            slippage_bps=assumptions.get("slippage_bps", 30),
            fee_bps=assumptions.get("fee_bps", 25),
            max_position_pct=assumptions.get("max_position_pct", 0.2),
            cooldown_s=assumptions.get("cooldown_s", 30),
        )
        self.last_ts: dict[str, float] = {}
        self.last_price: dict[str, float] = {}
        self.token_chain: dict[str, str] = {}
        self.decisions: list[dict] = []
        self.wallet_stats: dict[str, dict] = {}
        self.polls = 0
        self.started_ts = time.time()
        self.last_poll_ts: float | None = None
        self.last_poll_ms: int | None = None

    @property
    def addresses(self) -> list[str]:
        return [t["address"] for t in self.targets]

    @addresses.setter
    def addresses(self, value: list):
        self.targets = _targets(value, self.chain)

    async def poll_once(self) -> list[dict]:
        """One polling round across every followed wallet. Returns the new decisions it made, if any."""
        t0 = time.perf_counter()
        new_decisions = []
        for target in self.targets:
            address, chain = target["address"], target["chain"]
            stats = self.wallet_stats.setdefault(address, {"detected": 0, "mirrored": 0, "last_trade_ts": None, "errors": 0})
            try:
                rows = await self.client.wallet_trades(chain, address, max_pages=1)
            except Exception as e:
                stats["errors"] += 1
                new_decisions.append({"ts": time.time(), "wallet": address, "chain": chain, "action": "error", "reason": str(e)[:200]})
                continue
            normalized = w.normalize_trades(rows)
            if normalized:
                stats["last_trade_ts"] = max(stats["last_trade_ts"] or 0, normalized[-1]["ts"])
            since = self.last_ts.get(address, time.time() - 3600)
            fresh = sorted((t for t in normalized if t["ts"] > since), key=lambda t: t["ts"])
            for t in fresh:
                price = t["usd"] / t["qty"] if t["qty"] else 0.0
                if price <= 0:
                    continue
                token = t["token"]
                self.token_chain[token] = chain
                paper_usd = 0.0
                if t["kind"] == "buy":
                    paper_usd = self.portfolio.cash * self.assumptions.get("max_position_pct", 0.2)
                    filled = self.portfolio.buy(token, t["ts"], price, usd=paper_usd)
                    action = "buy" if filled else "skip_buy"
                else:
                    held = self.portfolio.positions.get(token)
                    paper_usd = held.qty * price if held else 0.0
                    filled = self.portfolio.sell(token, t["ts"], price, fraction=1.0)
                    action = "sell" if filled else "skip_sell"
                self.last_price[token] = price
                stats["detected"] += 1
                stats["mirrored"] += 1 if filled else 0
                detected = time.time()
                decision = {
                    "ts": t["ts"],
                    "detected_ts": detected,
                    "lag_s": round(max(detected - t["ts"], 0), 1),
                    "wallet": address,
                    "chain": chain,
                    "token": token,
                    "side": t["kind"],
                    "action": action,
                    "wallet_usd": round(t["usd"], 2),
                    "paper_usd": round(paper_usd, 2) if filled else 0.0,
                    "price": price,
                    "reason": f"mirrored {address[:8]}...'s {t['kind']}" if filled else self._skip_reason(t["kind"]),
                }
                self.decisions.append(decision)
                new_decisions.append(decision)
            if fresh:
                self.last_ts[address] = fresh[-1]["ts"]
        await self._resolve_tokens()
        # Enrich the persisted decision payload after the shared token cache has resolved. This
        # keeps follow feeds and later reports readable instead of reducing every token to an address.
        for decision in new_decisions:
            meta = tokens.get(decision.get("chain", self.chain), decision.get("token")) if decision.get("token") else {}
            decision["symbol"] = meta.get("symbol")
            decision["image"] = meta.get("image")
        await self._mark_to_market()
        self.portfolio.snapshot(time.time(), self.last_price)
        self.polls += 1
        self.last_poll_ts = time.time()
        self.last_poll_ms = round((time.perf_counter() - t0) * 1000)
        return new_decisions

    def _skip_reason(self, side: str) -> str:
        if side == "sell":
            return "wallet sold a token we don't hold"
        return "no room: cash, position cap or cooldown"

    async def _resolve_tokens(self):
        """Logos + symbols for every token we've seen (cached; a failure just leaves short addresses)."""
        if not hasattr(self.client, "tokens_multi"):
            return
        pairs = {(c, t) for t, c in self.token_chain.items()}
        if pairs:
            try:
                await tokens.resolve(self.client, pairs)
            except Exception:
                pass

    async def _mark_to_market(self):
        """Refreshes last_price for open positions from the onchain simple-price endpoint."""
        if not self.portfolio.positions or not hasattr(self.client, "token_prices"):
            return
        by_chain: dict[str, list[str]] = {}
        for token in self.portfolio.positions:
            by_chain.setdefault(self.token_chain.get(token, self.chain), []).append(token)
        for chain, addrs in by_chain.items():
            try:
                prices = await self.client.token_prices(chain, addrs)
            except CoinGeckoError:
                continue
            for a in addrs:
                if prices.get(a.lower()):
                    self.last_price[a] = prices[a.lower()]

    def positions(self) -> list[dict]:
        out = []
        for token, p in self.portfolio.positions.items():
            chain = self.token_chain.get(token, self.chain)
            meta = tokens.get(chain, token)
            price = self.last_price.get(token)
            value = p.qty * price if price else p.cost_usd
            unrealized = value - p.cost_usd
            realized = self.portfolio.realized_by_symbol.get(token, 0.0)
            out.append(
                {
                    "token": token,
                    "chain": chain,
                    "symbol": meta["symbol"],
                    "image": meta["image"],
                    "qty": p.qty,
                    "cost_usd": round(p.cost_usd, 2),
                    "price": price,
                    "value_usd": round(value, 2),
                    "pnl_usd": round(unrealized, 2),
                    "pnl_pct": round(unrealized / p.cost_usd * 100, 2) if p.cost_usd else None,
                    "unrealized_pnl_usd": round(unrealized, 2),
                    "realized_pnl_usd": round(realized, 2),
                    "total_pnl_usd": round(unrealized + realized, 2),
                    "opened_ts": p.opened_ts,
                }
            )
        return sorted(out, key=lambda x: -x["value_usd"])

    def feed(self, n: int = 60) -> list[dict]:
        out = []
        for d in reversed(self.decisions[-n:]):
            meta = tokens.get(d.get("chain", self.chain), d.get("token")) if d.get("token") else {}
            out.append({**d, "symbol": meta.get("symbol"), "image": meta.get("image")})
        return out

    def status(self) -> dict:
        lags = [d["lag_s"] for d in self.decisions if d.get("lag_s") is not None]
        curve = self.portfolio.equity_curve
        step = max(1, len(curve) // 500)
        return {
            "polls": self.polls,
            "label": self.label,
            "chain": self.chain,
            "assumptions": self.assumptions,
            "addresses": self.addresses,
            "targets": self.targets,
            "wallet_stats": self.wallet_stats,
            "metrics": self.portfolio.metrics(self.last_price),
            "cash_usd": round(self.portfolio.cash, 2),
            "budget_usd": self.budget_usd,
            "positions": {sym: {"qty": p.qty, "cost_usd": p.cost_usd} for sym, p in self.portfolio.positions.items()},
            "open_positions": self.positions(),
            "equity_curve": curve[::step][-500:],
            "recent_decisions": self.decisions[-20:],
            "feed": self.feed(),
            "latency_note": self.assumptions.get("latency_note", ""),
            "poll_s": self.poll_s,
            "started_ts": self.started_ts,
            "last_poll_ts": self.last_poll_ts,
            "last_poll_ms": self.last_poll_ms,
            "next_poll_ts": (self.last_poll_ts + self.poll_s) if self.last_poll_ts else None,
            "avg_lag_s": round(sum(lags) / len(lags), 1) if lags else None,
        }
