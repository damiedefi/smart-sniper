import json

from app.server import _report_details


def test_report_details_prioritises_active_tokens_and_keeps_chain_identity(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "metrics.json").write_text(json.dumps({"chain": "solana"}))
    (run / "decisions.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"chain": "solana", "token": "TOKEN-A", "side": "buy", "paper_usd": 20}),
                json.dumps({"chain": "solana", "token": "TOKEN-A", "side": "sell", "paper_usd": 0}),
                json.dumps({"chain": "solana", "token": "TOKEN-B", "side": "sell", "paper_usd": 0}),
            ]
        )
        + "\n"
    )
    details = _report_details(run)
    assert [row["token"] for row in details["token_rows"]] == ["TOKEN-A", "TOKEN-B"]
    assert all(row["chain"] == "solana" for row in details["token_rows"])
