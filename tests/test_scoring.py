"""Deterministic wallet scoring: skill score bounds and label pass-through."""
from app import scoring


def _trade(ts, kind, qty, usd):
    if kind == "buy":
        return {"kind": "buy", "to_token_address": "TOKEN", "to_token_amount": qty, "volume_in_usd": usd, "block_timestamp": ts}
    return {"kind": "sell", "from_token_address": "TOKEN", "from_token_amount": qty, "volume_in_usd": usd, "block_timestamp": ts}


def test_skill_score_is_bounded_0_to_100():
    metrics = {"win_rate": 1.0, "trades": 100}
    pnl = {"lifetime_realized_pnl_usd": 10_000_000}
    assert scoring.skill_score(metrics, pnl) == 100


def test_skill_score_zero_for_no_trades():
    assert scoring.skill_score({"win_rate": None, "trades": 0}, {"lifetime_realized_pnl_usd": 0}) == 0


def test_profile_wallet_picks_a_label_and_score():
    trades = [_trade(1000, "buy", 10, 100), _trade(2000, "sell", 10, 150), _trade(3000, "buy", 10, 100), _trade(4000, "sell", 10, 160)]
    result = scoring.profile_wallet(pnl_attrs={"total_realized_pnl_usd": 110, "token_stats": []}, trades_rows=trades, budget_usd=100, now=10_000)
    assert result["label"] in scoring.LABEL_DESCRIPTIONS
    assert 0 <= result["skill_score"] <= 100
    assert 0 <= result["copyability"] <= 100
