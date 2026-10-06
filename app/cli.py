"""`python -m app.cli <command>` -- the same commands the Makefile wraps (make backtest, forward, ...)."""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from core import links
from core.articlekit import build as build_article_kit
from core.assumptions import load as load_assumptions
from core.client import CoinGeckoClient
from core.plan import probe_capabilities
from core.recorder import Recorder, replay as replay_fixture
from core.report import build as build_report

from . import backtest as backtest_mod
from . import config, lucky, profile, runs, scan
from .follow import FollowEngine

COINGECKO_LINK_FILES = [
    "README.md",
    "AGENTS.md",
    "assumptions.yaml",
    "core/templates/article-draft.md",
]


async def cmd_backtest(args):
    client = CoinGeckoClient()
    await probe_capabilities(client)
    try:
        if args.wallets:
            addresses = [a.strip() for a in args.wallets.split(",") if a.strip()]
        else:
            scanned = await scan.scan(client, args.chain, args.source, n_tokens=config.DEFAULT_TOP_N_TOKENS)
            addresses = [c["address"] for c in scanned["candidates"] if not c["likely_bot"]][: args.max_wallets]
        print(f"[backtest] fetching trade history for {len(addresses)} wallet(s) on {args.chain}...")
        run_dir, m = await backtest_mod.run_live(client, args.chain, addresses, args.budget, load_assumptions(), max_pages=args.max_pages)
        blind, matched = m["blind"], m["size_matched"]
        result = {"candidate_wallets": m["candidate_wallets"], "selected_wallets": m["selected_wallets"]}
        print(f"[backtest] {result['candidate_wallets']} candidate wallet(s), {len(result['selected_wallets'])} selected")
        print(f"[backtest] blind full-size:  pnl={blind['pnl_pct']}%  trades={blind['trades']}  win_rate={blind['win_rate']}")
        print(f"[backtest] size-matched:     pnl={matched['pnl_pct']}%  trades={matched['trades']}  win_rate={matched['win_rate']}")
        print(f"[backtest] credits used: {client.credits_used}")
        print(f"[backtest] run: {run_dir}")
    finally:
        await client.close()


async def cmd_forward(args):
    client = CoinGeckoClient()
    await probe_capabilities(client)
    run_dir = None
    try:
        assumptions = load_assumptions()
        if args.wallets:
            addresses = [a.strip() for a in args.wallets.split(",") if a.strip()]
        else:
            scanned = await scan.scan(client, args.chain, args.source, n_tokens=config.DEFAULT_TOP_N_TOKENS)
            candidates = [c["address"] for c in scanned["candidates"] if not c["likely_bot"]][:30]
            profiles = await profile.profile_candidates(client, args.chain, candidates, args.budget)
            good = [p for p in profiles if not p.get("error") and p.get("copyability", 0) >= 30]
            good.sort(key=lambda p: p["skill_score"], reverse=True)
            addresses = [p["address"] for p in good[: args.top_k]]
        print(f"[forward] following {len(addresses)} wallet(s) on {args.chain} for {args.minutes} minute(s), polling every {args.poll_s}s")
        engine = FollowEngine(client, args.chain, addresses, args.budget, assumptions, args.poll_s)
        run_dir = runs.new_run_dir("forward")
        deadline = time.time() + args.minutes * 60
        while time.time() < deadline:
            new = await engine.poll_once()
            for d in new:
                runs.append_decision(run_dir, d)
                print(f"  [{d.get('action')}] {d.get('token')} via {str(d.get('wallet'))[:10]}...")
            await asyncio.sleep(min(args.poll_s, max(deadline - time.time(), 0)))
        status = engine.status()
        runs.write_metrics(run_dir, {"mode": "forward", "chain": args.chain, "addresses": addresses, "credits_used": client.credits_used, **status})
        runs.write_equity(run_dir, engine.portfolio.equity_curve)
        runs.write_trades_csv(run_dir, engine.portfolio.closed)
        print(f"[forward] polls={status['polls']}  trades={status['metrics']['trades']}  pnl={status['metrics']['pnl_pct']}%")
        print(f"[forward] credits used: {client.credits_used}")
        print(f"[forward] run: {run_dir}")
    finally:
        # Keep a reportable snapshot if an outer supervisor interrupts during the
        # final poll/sleep. The supervisor normally allows bounded runs to finish,
        # but this also makes Ctrl-C/restarts recoverable for a human-run command.
        if run_dir is not None and not (Path(run_dir) / "metrics.json").exists():
            status = engine.status()
            runs.write_metrics(
                run_dir,
                {
                    "mode": "forward",
                    "chain": args.chain,
                    "addresses": addresses,
                    "credits_used": client.credits_used,
                    "interrupted": True,
                    **status,
                },
            )
            runs.write_equity(run_dir, engine.portfolio.equity_curve)
            runs.write_trades_csv(run_dir, engine.portfolio.closed)
        await client.close()


async def cmd_autopilot(args):
    from . import autopilot

    await autopilot.run(
        chain=args.chain,
        source=args.source,
        budget_usd=args.budget,
        top_k=args.top_k,
        rescan_hours=args.rescan_hours,
        poll_s=args.poll_s,
        max_credits_per_day=args.max_credits_per_day,
        max_seconds=args.seconds,
    )


def cmd_report(args):
    run_id = runs.resolve_run_id(args.run)
    run_dir = Path(config.RUNS_DIR) / run_id
    metrics = runs.read_metrics(run_dir)
    equity = runs.read_equity(run_dir)
    if not metrics:
        print(f"no metrics.json in {run_dir}", file=sys.stderr)
        sys.exit(1)
    scenario_metrics = metrics.get("blind") or metrics.get("metrics") or metrics
    paths = build_report(run_id, scenario_metrics, equity, credits_used=metrics.get("credits_used", 0), out_dir=run_dir.parent)
    print(f"[report] {paths['html']}")


def cmd_article_kit(args):
    run_id = runs.resolve_run_id(args.run)
    run_dir = Path(config.RUNS_DIR) / run_id
    metrics = runs.read_metrics(run_dir)
    equity = runs.read_equity(run_dir)
    scenario_metrics = metrics.get("blind") or metrics.get("metrics") or metrics
    paths = build_article_kit(
        title=f"Smart Money Radar: {run_id}",
        handle=args.handle or "yourhandle",
        metrics=scenario_metrics,
        equity_curve=equity,
        credits_used=metrics.get("credits_used", 0),
        out_dir=run_dir / "article-kit",
        screenshot_urls=args.screenshot_urls.split(",") if args.screenshot_urls else None,
    )
    print(json.dumps(paths, indent=2))


def cmd_set_link(args):
    handle = args.handle
    files = [f for f in COINGECKO_LINK_FILES if Path(f).exists()]
    links.set_link(files, handle, source="github", url_override=args.url)
    print(f"[set-link] tagged {len(files)} file(s) with utm_content={handle.lstrip('@').lower()}")


async def cmd_record(args):
    client = CoinGeckoClient()
    await probe_capabilities(client)
    try:
        rec = Recorder("radar", args.tag)
        print(f"[record] writing {rec.path} for {args.seconds}s ... (Ctrl-C to stop early)")
        assumptions = load_assumptions()
        addresses = [a.strip() for a in args.wallets.split(",") if a.strip()] if args.wallets else []
        engine = FollowEngine(client, args.chain, addresses, args.budget, assumptions, args.poll_s)
        deadline = time.time() + args.seconds
        while time.time() < deadline:
            for d in await engine.poll_once():
                rec.write("decision", d)
            rec.write("status", engine.status())
            await asyncio.sleep(min(args.poll_s, max(deadline - time.time(), 0)))
        rec.close()
        print(f"[record] saved {rec.name}")
    finally:
        await client.close()


async def cmd_replay(args):
    print(f"[replay] {args.file}")
    async for ev, data in replay_fixture(args.file, speed=args.speed):
        print(f"  [{ev}] {json.dumps(data)[:200]}")


async def cmd_lucky(args):
    await lucky.main(args.chain, args.source, args.max_wallets, args.tokens)


def main():
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backtest")
    p.add_argument("--chain", default="solana")
    p.add_argument("--source", default="trending_1h")
    p.add_argument("--wallets", default="")
    p.add_argument("--budget", type=float, default=config.DEFAULT_BUDGET_USD)
    p.add_argument("--max-wallets", type=int, default=5)
    p.add_argument("--max-pages", type=int, default=10)
    p.set_defaults(func=cmd_backtest, is_async=True)

    p = sub.add_parser("forward")
    p.add_argument("--chain", default="solana")
    p.add_argument("--source", default="trending_1h")
    p.add_argument("--wallets", default="")
    p.add_argument("--budget", type=float, default=config.DEFAULT_BUDGET_USD)
    p.add_argument("--top-k", type=int, default=config.DEFAULT_TOP_K_FOLLOW)
    p.add_argument("--minutes", type=float, default=3)
    p.add_argument("--poll-s", type=float, default=config.DEFAULT_FOLLOW_POLL_S)
    p.set_defaults(func=cmd_forward, is_async=True)

    p = sub.add_parser("autopilot")
    p.add_argument("--chain", default="solana")
    p.add_argument("--source", default="trending_1h")
    p.add_argument("--budget", type=float, default=config.DEFAULT_BUDGET_USD)
    p.add_argument("--top-k", type=int, default=config.DEFAULT_TOP_K_FOLLOW)
    p.add_argument("--rescan-hours", type=float, default=config.DEFAULT_RESCAN_HOURS)
    p.add_argument("--poll-s", type=float, default=config.DEFAULT_FOLLOW_POLL_S)
    p.add_argument("--max-credits-per-day", type=float, default=config.DEFAULT_MAX_CREDITS_PER_DAY)
    p.add_argument("--seconds", type=float, default=None, help="stop cleanly after N seconds (used by unattended evaluation)")
    p.set_defaults(func=cmd_autopilot, is_async=True)

    p = sub.add_parser("lucky", help="lucky or good? filter the wallets behind trending pools")
    p.add_argument("--chain", default=config.DEFAULT_CHAIN)
    p.add_argument("--source", default="trending_24h", choices=list(config.SOURCES))
    p.add_argument("--max-wallets", type=int, default=100)
    p.add_argument("--tokens", type=int, default=config.DEFAULT_TOP_N_TOKENS)
    p.set_defaults(func=cmd_lucky, is_async=True)

    p = sub.add_parser("report")
    p.add_argument("--run", default="latest")
    p.set_defaults(func=cmd_report, is_async=False)

    p = sub.add_parser("article-kit")
    p.add_argument("--run", default="latest")
    p.add_argument("--handle", default="")
    p.add_argument("--screenshot-urls", default="")
    p.set_defaults(func=cmd_article_kit, is_async=False)

    p = sub.add_parser("set-link")
    p.add_argument("--handle", required=True)
    p.add_argument("--url", default=None)
    p.set_defaults(func=cmd_set_link, is_async=False)

    p = sub.add_parser("record")
    p.add_argument("--chain", default="solana")
    p.add_argument("--wallets", default="")
    p.add_argument("--budget", type=float, default=config.DEFAULT_BUDGET_USD)
    p.add_argument("--poll-s", type=float, default=5)
    p.add_argument("--seconds", type=float, default=60)
    p.add_argument("--tag", default="session")
    p.set_defaults(func=cmd_record, is_async=True)

    p = sub.add_parser("replay")
    p.add_argument("--file", required=True)
    p.add_argument("--speed", type=float, default=1.0)
    p.set_defaults(func=cmd_replay, is_async=True)

    args = parser.parse_args()
    if args.is_async:
        asyncio.run(args.func(args))
    else:
        args.func(args)


if __name__ == "__main__":
    main()
