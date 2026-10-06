"""Every backtest / forward / autopilot run writes decisions.jsonl, trades.csv and metrics.json under runs/<id>/.

This module is the one place that knows that layout, so the CLI, the server and `make report` agree on it.
"""
import csv
import json
import time
from pathlib import Path

from . import config


def new_run_dir(mode: str, base: str | Path = config.RUNS_DIR) -> Path:
    """Creates and returns runs/<timestamp>-<mode>/."""
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{mode}"
    path = Path(base) / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_metrics(run_dir: str | Path, metrics: dict):
    (Path(run_dir) / "metrics.json").write_text(json.dumps(metrics, indent=2, default=str))


def read_metrics(run_dir: str | Path) -> dict:
    path = Path(run_dir) / "metrics.json"
    return json.loads(path.read_text()) if path.exists() else {}


def write_decisions(run_dir: str | Path, decisions: list[dict]):
    path = Path(run_dir) / "decisions.jsonl"
    with path.open("w") as fh:
        for d in decisions:
            fh.write(json.dumps(d, default=str) + "\n")


def append_decision(run_dir: str | Path, decision: dict):
    path = Path(run_dir) / "decisions.jsonl"
    with path.open("a") as fh:
        fh.write(json.dumps(decision, default=str) + "\n")


def write_trades_csv(run_dir: str | Path, closed_trades: list):
    """closed_trades: core.paper.ClosedTrade instances or equivalent dicts."""
    path = Path(run_dir) / "trades.csv"
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["symbol", "qty", "buy_usd", "sell_usd", "pnl_usd", "opened_ts", "closed_ts"])
        for t in closed_trades:
            row = t.__dict__ if hasattr(t, "__dict__") else t
            writer.writerow([row["symbol"], row["qty"], row["buy_usd"], row["sell_usd"], row["pnl_usd"], row["opened_ts"], row["closed_ts"]])


def write_equity(run_dir: str | Path, equity_curve: list[tuple[float, float]]):
    (Path(run_dir) / "equity.json").write_text(json.dumps(equity_curve))


def read_equity(run_dir: str | Path) -> list[tuple[float, float]]:
    path = Path(run_dir) / "equity.json"
    return json.loads(path.read_text()) if path.exists() else []


def _downsample(values: list[float], n: int = 48) -> list[float]:
    if len(values) <= n:
        return [round(v, 4) for v in values]
    step = (len(values) - 1) / (n - 1)
    return [round(values[round(i * step)], 4) for i in range(n)]


def summarize(path: Path) -> dict:
    """One run card: mode, chain, P&L, win rate, drawdown, trades, credits, window and an equity sparkline."""
    metrics = read_metrics(path)
    core = metrics.get("blind") or metrics.get("metrics") or metrics
    equity = read_equity(path)
    window = metrics.get("covered_window") or {}
    start = window.get("start_ts") or (equity[0][0] if equity else None)
    end = window.get("end_ts") or (equity[-1][0] if equity else None)
    wallets = metrics.get("selected_wallets") or metrics.get("addresses") or []
    return {
        "id": path.name,
        "mode": metrics.get("mode") or path.name.rsplit("-", 1)[-1],
        "chain": metrics.get("chain"),
        "pnl_usd": core.get("pnl_usd"),
        "pnl_pct": core.get("pnl_pct"),
        "win_rate": core.get("win_rate"),
        "max_drawdown_pct": core.get("max_drawdown_pct"),
        "trades": core.get("trades"),
        "credits_used": metrics.get("credits_used"),
        "size_matched": metrics.get("size_matched"),
        "wallets": len(wallets),
        "start_ts": start,
        "end_ts": end,
        "interrupted": metrics.get("interrupted", False),
        "spark": _downsample([e for _, e in equity]),
        "has_metrics": bool(metrics),
        "has_report": (path / "report.html").exists(),
        "has_article_kit": (path / "article-kit" / "article-draft.md").exists(),
    }


def list_runs(base: str | Path = config.RUNS_DIR) -> list[dict]:
    """Every run directory, newest first, with its metrics and whether a report/article-kit already exists."""
    root = Path(base)
    if not root.exists():
        return []
    out = []
    for path in sorted(root.iterdir(), reverse=True):
        if not path.is_dir():
            continue
        out.append(
            {
                "id": path.name,
                "mode": path.name.rsplit("-", 1)[-1],
                "metrics": read_metrics(path),
                "has_report": (path / "report.html").exists(),
                "has_article_kit": (path / "article-kit").exists(),
            }
        )
    return out


def run_cards(base: str | Path = config.RUNS_DIR) -> list[dict]:
    """summarize() for every run directory, newest first."""
    root = Path(base)
    if not root.exists():
        return []
    return [summarize(p) for p in sorted(root.iterdir(), reverse=True) if p.is_dir()]


def safe_run_dir(run_id: str, base: str | Path = config.RUNS_DIR) -> Path | None:
    """runs/<run_id> if run_id is a plain directory name that exists, else None (no path traversal)."""
    if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
        return None
    path = Path(base) / run_id
    return path if path.is_dir() else None


def resolve_run_id(run_id: str, base: str | Path = config.RUNS_DIR) -> str:
    """Resolves 'latest' to the newest run directory name; otherwise returns run_id unchanged."""
    if run_id != "latest":
        return run_id
    runs = list_runs(base)
    if not runs:
        raise FileNotFoundError("no runs yet")
    return runs[0]["id"]
