"""Autopilot: rescans on a schedule, keeps the top K copyable wallets, and paper copy-trades them 24/7."""
import asyncio
import logging
import time

from core.assumptions import load as load_assumptions
from core.autopilot import Autopilot, Job
from core.client import CoinGeckoClient
from core.plan import probe_capabilities
from core.store import Store

from . import config, profile, runs, scan

log = logging.getLogger("app.autopilot")


async def rescan_and_select(client: CoinGeckoClient, chain: str, source: str, budget_usd: float, top_k: int) -> list[str]:
    """Rescans, profiles the candidates, and keeps the top K copyable, non-bot wallets."""
    scanned = await scan.scan(client, chain, source, n_tokens=config.DEFAULT_TOP_N_TOKENS)
    candidates = [c["address"] for c in scanned["candidates"] if not c["likely_bot"]][: config.DEFAULT_WALLETS_PROFILED]
    if not candidates:
        return []
    profiles = await profile.profile_candidates(client, chain, candidates, budget_usd)
    good = [p for p in profiles if not p.get("error") and p.get("label") in ("proven_trader", "whale") and p.get("copyability", 0) >= 40]
    good.sort(key=lambda p: (p["skill_score"], p["copyability"]), reverse=True)
    return [p["address"] for p in good[:top_k]]


async def run(
    chain: str = "solana",
    source: str = "trending_1h",
    budget_usd: float = config.DEFAULT_BUDGET_USD,
    top_k: int = config.DEFAULT_TOP_K_FOLLOW,
    rescan_hours: float = config.DEFAULT_RESCAN_HOURS,
    poll_s: float = config.DEFAULT_FOLLOW_POLL_S,
    max_credits_per_day: float = config.DEFAULT_MAX_CREDITS_PER_DAY,
    store_path: str = "state.db",
    max_seconds: float | None = None,
    live: dict | None = None,
):
    """Runs forever, or for max_seconds when set, with periodic rescan/poll/recap jobs.

    `live`, if given, is filled with {"run_dir", "engine", "client"} so a UI can show progress.
    """
    from .follow import FollowEngine  # local import: avoids a cycle with app.server at module load time

    client = CoinGeckoClient()
    await probe_capabilities(client)
    store = Store(store_path)
    run_dir = runs.new_run_dir("autopilot")
    assumptions = load_assumptions()
    engine = FollowEngine(client, chain, store.get("followed", []), budget_usd, assumptions, poll_s)
    engine.addresses = store.get("followed", [])
    if live is not None:
        live.update(run_dir=run_dir, engine=engine, client=client, chain=chain, started_ts=time.time())

    async def do_rescan():
        picked = await rescan_and_select(client, chain, source, budget_usd, top_k)
        current = set(engine.addresses)
        added = [a for a in picked if a not in current]
        dropped = [a for a in engine.addresses if a not in picked]
        engine.addresses = picked
        store.set("followed", picked)
        store.log_decision("rescan", {"picked": picked, "added": added, "dropped": dropped})
        log.info("rescan: following %d wallets (+%d -%d)", len(picked), len(added), len(dropped))

    async def do_poll():
        if not engine.addresses:
            return
        for d in await engine.poll_once():
            store.log_decision("trade", d)
        runs.write_metrics(run_dir, {"mode": "autopilot", "credits_used": client.credits_used, **engine.status()})

    async def do_recap():
        status = engine.status()
        recap = (
            f"# While you slept\n\n"
            f"- Followed wallets: {len(engine.addresses)}\n"
            f"- Trades: {status['metrics']['trades']}\n"
            f"- PnL: {status['metrics']['pnl_usd']} USD ({status['metrics']['pnl_pct']}%)\n"
            f"- Credits used: {client.credits_used}\n"
        )
        (run_dir / "recap.md").write_text(recap)
        log.info("daily recap written to %s", run_dir / "recap.md")

    if not engine.addresses:
        await do_rescan()

    pilot = Autopilot(
        jobs=[
            Job("rescan", rescan_hours * 3600, do_rescan),
            Job("poll", poll_s, do_poll),
            Job("recap", 24 * 3600, do_recap),
        ],
        store=store,
        credits_today_fn=lambda: client.credits_used,
        max_credits_per_day=max_credits_per_day,
    )
    try:
        if max_seconds is None:
            await pilot.run_forever()
        else:
            try:
                await asyncio.wait_for(pilot.run_forever(), timeout=max_seconds)
            except asyncio.TimeoutError:
                pass
    finally:
        status = engine.status()
        runs.write_metrics(run_dir, {"mode": "autopilot", "chain": chain, "credits_used": client.credits_used, **status})
        runs.write_equity(run_dir, engine.portfolio.equity_curve)
        await client.close()
        store.close()
