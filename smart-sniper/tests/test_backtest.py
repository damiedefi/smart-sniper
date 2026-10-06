"""Backtest: no-lookahead wallet selection, and blind full-size vs size-matched sizing."""
from app import backtest


def _trade(ts, kind, qty, usd):
    if kind == "buy":
        return {"kind": "buy", "to_token_address": "TOKEN", "to_token_amount": qty, "volume_in_usd": usd, "block_timestamp": ts}
    return {"kind": "sell", "from_token_address": "TOKEN", "from_token_amount": qty, "volume_in_usd": usd, "block_timestamp": ts}


ASSUMPTIONS = {"slippage_bps": 0, "fee_bps": 0, "starting_cash": 100, "max_position_pct": 0.5, "cooldown_s": 0}


def test_select_wallet_only_uses_train_slice():
    # Losing trades early (train), one huge winner right at the end (test). Should NOT select:
    # the wallet only looks good in the slice we're not allowed to look at yet.
    rows = [_trade(t, "buy", 1, 100) for t in range(0, 100, 10)] + [_trade(t, "sell", 1, 50) for t in range(1, 100, 10)]
    rows += [_trade(9990, "buy", 1, 100), _trade(9999, "sell", 1, 100_000)]
    events = backtest.trades_to_events("0xW", rows)
    window = backtest.select_wallet("0xW", events, train_frac=0.6)
    assert window.selected is False  # the train slice alone is a losing record


def test_select_wallet_selects_a_genuinely_good_train_record():
    rows = []
    for i, ts in enumerate(range(0, 100, 10)):
        rows.append(_trade(ts, "buy", 1, 100))
        rows.append(_trade(ts + 1, "sell", 1, 150))
    events = backtest.trades_to_events("0xGOOD", rows)
    window = backtest.select_wallet("0xGOOD", events, train_frac=0.6)
    assert window.selected is True
    assert window.train_metrics["win_rate"] == 1.0


def test_blind_scenario_mirrors_wallet_trade_size_capped_by_cash():
    # max_position_pct=1.0 here so the only cap in play is available cash, not position sizing.
    uncapped = {**ASSUMPTIONS, "max_position_pct": 1.0}
    events = [
        backtest.Event(ts=1, kind="wallet_trade", data={"token": "TOKEN", "kind": "buy", "qty": 1000, "usd": 90, "ts": 1}),
    ]
    portfolio = backtest.replay_scenario(events, "blind", budget_usd=100, assumptions=uncapped)
    assert portfolio.positions["TOKEN"].cost_usd == 90


def test_size_matched_scenario_ignores_wallet_trade_size():
    # The wallet spent $90,000 -- a size-matched copy at a $100 budget should size to the budget, not the wallet.
    events = [
        backtest.Event(ts=1, kind="wallet_trade", data={"token": "TOKEN", "kind": "buy", "qty": 1000, "usd": 90_000, "ts": 1}),
    ]
    portfolio = backtest.replay_scenario(events, "size_matched", budget_usd=100, assumptions=ASSUMPTIONS)
    assert portfolio.positions["TOKEN"].cost_usd == 50  # 100 * max_position_pct(0.5)


def test_run_reports_both_scenarios():
    rows = []
    for ts in range(0, 100, 10):
        rows.append(_trade(ts, "buy", 1, 10))
        rows.append(_trade(ts + 1, "sell", 1, 15))
    result = backtest.run({"0xGOOD": rows}, budget_usd=100, assumptions=ASSUMPTIONS)
    assert "blind" in result["scenarios"] and "size_matched" in result["scenarios"]
    assert result["covered_window"]["trades"] > 0
