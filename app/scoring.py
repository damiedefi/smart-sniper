"""Deterministic, explainable wallet scoring shared by the Radar, the Wallet profile and the backtest.

Every number here comes from core.wallets (FIFO-matched trades + the wallet's own lifetime PnL).
No black box: a skill score is win rate, lifetime realized PnL, and sample size, weighted and
capped so one huge trade can't dominate. Change the weights in core.wallets.skill_score().
"""
from core import wallets as w


def skill_score(match_metrics: dict, pnl_features: dict) -> int:
    """0-100. See core.wallets.skill_score for the formula."""
    return w.skill_score(match_metrics, pnl_features)


def profile_wallet(
    pnl_attrs: dict,
    trades_rows: list[dict],
    budget_usd: float,
    now: float | None = None,
    balances_attrs: dict | None = None,
    address: str | None = None,
) -> dict:
    """Turns raw wallet_pnl + wallet_trades (+ balances) rows into the fields every surface needs."""
    normalized = w.normalize_trades(trades_rows)
    closed = w.fifo_matches(normalized)
    match_metrics = w.match_metrics(closed)
    pnl_feat = w.pnl_features(pnl_attrs)
    portfolio = w.portfolio_features(balances_attrs)
    activity = w.activity_features(trades_rows, now=now)
    label = w.label_wallet(match_metrics, pnl_feat, activity, address=address)
    copy_score = w.copyability(match_metrics.get("median_trade_usd") or pnl_feat.get("avg_trade_usd"), activity.get("recent_trades_per_day"), budget_usd)
    infra_name = w.known_infra(address) if address else None
    return {
        "label": label,
        "label_rule": LABEL_DESCRIPTIONS.get(label, ""),
        "skill_score": skill_score(match_metrics, pnl_feat),
        "copyability": copy_score,
        "copyable": w.is_copyable(copy_score) and label not in ("bot_like", "protocol"),
        "style": w.trading_style(match_metrics, activity),
        "tags": w.wallet_tags(pnl_feat, portfolio, activity, match_metrics),
        "is_infra": label == "protocol",
        "infra_name": infra_name,
        "infra_note": (
            f"Matches a known infrastructure contract: {infra_name}."
            if infra_name
            else "Trade volume and multi-chain activity look like a contract (router, pool, or similar), not a personal wallet. This is a heuristic, not a confirmed identity."
            if label == "protocol"
            else None
        ),
        "days_since_last_trade": activity.get("days_since_last_trade"),
        "recent_trades_per_day": activity.get("recent_trades_per_day"),
        "match_metrics": match_metrics,
        "pnl_features": pnl_feat,
        "portfolio": portfolio,
        "activity": activity,
        "closed_trades": closed,
    }


LABEL_DESCRIPTIONS = {
    "proven_trader": "Wins more than it loses, with a real sample of trades behind it.",
    "one_hit": "Only one closed trade so far, not enough to judge yet.",
    "whale": "Large lifetime PnL, but its trade sizes may be too big to copy at a small budget.",
    "bot_like": "Trades far more often than a human would; likely automated.",
    "flipper": "Trades often, in and out fast.",
    "dormant": "Hasn't traded in over two weeks.",
    "insider_like": "Holds a big slice of supply it never bought.",
    "fresh_wallet": "Has only traded a handful of tokens.",
    "protocol": "Likely a contract (router, pool, or similar infrastructure), not a personal trading wallet.",
    "holder": "Holds the token; not enough history to say more.",
}
