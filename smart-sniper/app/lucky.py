"""Lucky or good? A wallet filter on top of Smart Money Radar.

The idea: a wallet can top a token's leaderboard off one great trade. That's luck, not skill, and
it's the wallet you least want to copy. So this starts from the pools trending on a chain, pulls
the traders behind them, fetches each wallet's PnL across every chain it trades, and keeps only the
wallets whose profit is spread across many positions.

What comes from CoinGecko API: trending pools, top traders, recent pool trades and wallet PnL.
What this file decides: the keep/cut verdict, the thresholds and the reasons. Those are my own
rules, not CoinGecko fields. Change RULES below to make them yours.

Run it:  make lucky            (defaults: Robinhood Chain, trending 24h)
         make lucky CHAIN=base SOURCE=trending_1h WALLETS=40
"""
import asyncio
import html
import json
import time
from pathlib import Path

from core import config as core_config
from core import wallets as w
from core.client import CoinGeckoClient, CoinGeckoError, PlanRestrictedError

from . import config, scan
from .profile import wallet_networks

# ---- the rules (edit these) ----
RULES = {
    # Cut a wallet if its single best token is more than this share of all its profitable positions.
    "max_profit_concentration": 0.50,
    # Need at least this many closed positions before a win rate means anything.
    "min_tokens_sold": 5,
    # Of the positions it closed, at least this share must have been profitable.
    "min_win_rate": 0.50,
    # More trades than this on its own is a bot or market maker, not a person picking entries.
    "max_trades": 5000,
}

# Tokens that aren't ideas: base assets and stablecoins almost every wallet holds.
NOT_IDEAS = {"WETH", "ETH", "USDC", "USDT", "USDG", "DAI", "WBTC", "USDC.E", "USDBC", "WBNB", "BNB", "WAVAX", "WPOL", "MATIC", "WMATIC", "CBBTC"}

MAX_CONCURRENT_WALLETS = 8
BRAND_SVG = Path(__file__).resolve().parent.parent / "core" / "brand" / "coingecko-api-on-dark.svg"


def best_token(pnl_attrs: dict | None) -> dict | None:
    """The position with the largest realized profit, as {symbol, network, realized_usd}."""
    stats = (pnl_attrs or {}).get("token_stats") or []
    best = None
    for s in stats:
        realized = w._f(s.get("realized_pnl_usd"), 0.0) or 0.0
        if realized > 0 and (best is None or realized > best["realized_usd"]):
            best = {
                "symbol": s.get("symbol") or scan._short(s.get("address")),
                "network": s.get("network"),
                "realized_usd": round(realized, 0),
            }
    return best


def holdings(pnl_attrs: dict | None) -> list[dict]:
    """Positions the wallet still holds (open PnL on a token it bought), excluding base assets."""
    out = []
    for s in (pnl_attrs or {}).get("token_stats") or []:
        sym = str(s.get("symbol") or "")
        if sym.upper() in NOT_IDEAS:
            continue
        if (w._f(s.get("unrealized_pnl_usd"), 0.0) or 0) != 0 and (s.get("total_buy_count") or 0) > 0:
            out.append({"symbol": sym, "network": s.get("network"), "address": str(s.get("address") or "").lower(), "bought_usd": w._f(s.get("total_buy_usd"), 0.0) or 0.0})
    return out


def ideas(rows: list[dict], min_wallets: int = 2, top: int = 10) -> list[dict]:
    """Tokens that two or more of the kept wallets are holding right now. A starting point for research,
    not a signal: wallets found through the same trending token will naturally overlap on it."""
    agg: dict[str, dict] = {}
    for r in rows:
        if not r["verdict"]["keep"]:
            continue
        for h in r.get("holdings") or []:
            k = f"{h['network']}:{h['address']}"
            e = agg.setdefault(k, {"symbol": h["symbol"], "network": h["network"], "wallets": 0, "bought_usd": 0.0})
            e["wallets"] += 1
            e["bought_usd"] += h["bought_usd"]
    out = [e for e in agg.values() if e["wallets"] >= min_wallets]
    out.sort(key=lambda e: (-e["wallets"], -e["bought_usd"]))
    return [{**e, "bought_usd": round(e["bought_usd"], 0)} for e in out[:top]]


def verdict(features: dict, rules: dict = RULES) -> dict:
    """Keep or cut one wallet from its PnL features. Pure function, no network, easy to test.

    Checks run in order and the first one that fails is the reason shown.
    """
    if not features.get("available"):
        return {"keep": False, "check": "no_data", "reason": "No PnL data returned for this wallet"}
    if not features.get("pnl_reliable", True):
        return {"keep": False, "check": "unreliable", "reason": "PnL skewed by a mispriced token, can't trust it"}
    trades = (features.get("total_buys") or 0) + (features.get("total_sells") or 0)
    if w.looks_like_infra_contract(features, {}) or trades > rules["max_trades"]:
        return {"keep": False, "check": "bot", "reason": f"{trades:,} trades, looks like a bot or market maker"}
    realized = features.get("lifetime_realized_pnl_usd") or 0
    if realized <= 0:
        return {"keep": False, "check": "not_profitable", "reason": "Not in profit on closed positions"}
    sold = features.get("tokens_sold") or 0
    if sold < rules["min_tokens_sold"]:
        return {"keep": False, "check": "too_few", "reason": f"Only {sold} closed position(s), too few to judge"}
    conc = features.get("profit_concentration")
    if conc is not None and conc > rules["max_profit_concentration"]:
        return {"keep": False, "check": "one_hit", "reason": f"One token is {round(conc * 100)}% of its profit"}
    win = features.get("win_rate_tokens")
    if win is not None and win < rules["min_win_rate"]:
        return {"keep": False, "check": "low_win_rate", "reason": f"Wins on only {round(win * 100)}% of closed positions"}
    spread = f"{round(conc * 100)}%" if conc is not None else "n/a"
    wr = f"{round(win * 100)}%" if win is not None else "n/a"
    return {"keep": True, "check": "pass", "reason": f"Profit spread out (top token {spread}), wins {wr} of {sold} closed positions"}


def summarize(rows: list[dict]) -> dict:
    """Headline counts for the terminal and the report card."""
    checks: dict[str, int] = {}
    for r in rows:
        checks[r["verdict"]["check"]] = checks.get(r["verdict"]["check"], 0) + 1
    kept = [r for r in rows if r["verdict"]["keep"]]
    return {
        "wallets_checked": len(rows),
        "kept": len(kept),
        "cut": len(rows) - len(kept),
        "one_hit_wonders": checks.get("one_hit", 0),
        "bots": checks.get("bot", 0),
        "by_check": checks,
    }


async def _pnl_for(client: CoinGeckoClient, chain: str, address: str, sem: asyncio.Semaphore) -> tuple[str, dict | None]:
    """One wallet's PnL across its VM family. Returns (status, attrs)."""
    async with sem:
        try:
            attrs = await client.wallet_pnl(address, wallet_networks(address, chain), per_page=200, sort="total_buy_usd_desc")
            return "ok", attrs
        except PlanRestrictedError:
            return "locked", None
        except CoinGeckoError:
            return "error", None


async def run(client: CoinGeckoClient, chain: str, source: str, max_wallets: int, n_tokens: int, rules: dict = RULES) -> dict:
    """Scan -> candidate wallets -> PnL -> verdicts. Returns everything the report needs."""
    scanned = await scan.scan(client, chain, source, n_tokens=n_tokens)
    candidates = [c for c in scanned["candidates"] if not c["likely_bot"] and not c["known_infra"] and w.address_kind(c["address"])]
    # Candidates arrive sorted by how many tokens they appear in, which floats bots to the top.
    # Sample evenly across the list instead so the run reflects the whole field.
    if len(candidates) > max_wallets:
        step = len(candidates) / max_wallets
        candidates = [candidates[int(i * step)] for i in range(max_wallets)]

    sem = asyncio.Semaphore(MAX_CONCURRENT_WALLETS)
    results = await asyncio.gather(*(_pnl_for(client, chain, c["address"], sem) for c in candidates))

    rows = []
    locked = 0
    for c, (status, attrs) in zip(candidates, results):
        if status == "locked":
            locked += 1
            continue
        features = w.pnl_features(attrs) if status == "ok" else {"available": False}
        rows.append(
            {
                "address": c["address"],
                "short": c["short"],
                "seen_in": [s["symbol"] for s in c["seen_in"]],
                "features": features,
                "best_token": best_token(attrs),
                "holdings": holdings(attrs),
                "verdict": verdict(features, rules),
            }
        )

    return {
        "chain": chain,
        "source": source,
        "rules": rules,
        "tokens": [{"symbol": t["symbol"], "address": t["address"]} for t in scanned["tokens"]],
        "rows": rows,
        "summary": summarize(rows),
        "ideas": ideas(rows),
        "plan_locked_wallets": locked,
        "credits_used": getattr(client, "credits_used", None),
        "generated_at": int(time.time()),
    }


# ---- output ----


def _money(v) -> str:
    if v is None:
        return "n/a"
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000:
        return f"{sign}${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{sign}${v / 1_000:.1f}K"
    return f"{sign}${v:.0f}"


def print_summary(result: dict, run_dir: Path):
    s = result["summary"]
    print()
    print(f"  Smart Sniper: {core_config.CHAINS.get(result['chain'], result['chain'])}, {config.SOURCES.get(result['source'], result['source'])}")
    print(f"  Tokens scanned:   {len(result['tokens'])}")
    print(f"  Wallets checked:  {s['wallets_checked']}")
    print(f"  Kept:             {s['kept']}")
    print(f"  Cut:              {s['cut']}  (bots: {s['bots']}, one-hit wonders: {s['one_hit_wonders']})")
    if result["plan_locked_wallets"]:
        print(f"  Skipped {result['plan_locked_wallets']} wallet(s): wallet endpoints need the Analyst plan.")
    print()
    for r in sorted(result["rows"], key=lambda r: (not r["verdict"]["keep"], -(r["features"].get("lifetime_realized_pnl_usd") or 0)))[:15]:
        mark = "KEEP" if r["verdict"]["keep"] else "CUT "
        print(f"  {mark}  {r['short']:<14} {_money(r['features'].get('lifetime_realized_pnl_usd')):>9}  {r['verdict']['reason']}")
    if result.get("ideas"):
        print()
        print("  Held by 2+ kept wallets right now:")
        for i in result["ideas"]:
            print(f"    {i['symbol']:<12} {i['network']:<10} {i['wallets']} wallets   {_money(i['bought_usd'])} bought")
    print()
    print(f"  Report: {run_dir / 'report.html'}")
    print(f"  Data:   {run_dir / 'results.json'}")
    print()


def _row_html(r: dict) -> str:
    f = r["features"]
    keep = r["verdict"]["keep"]
    best = r["best_token"]
    best_txt = f"{html.escape(str(best['symbol']))} {_money(best['realized_usd'])}" if best else "n/a"
    return (
        f'<tr class="{"keep" if keep else "cut"}">'
        f'<td class="tag">{"KEEP" if keep else "CUT"}</td>'
        f"<td class=\"mono\">{html.escape(r['short'])}</td>"
        f"<td class=\"num\">{_money(f.get('lifetime_realized_pnl_usd'))}</td>"
        f"<td>{best_txt}</td>"
        f"<td>{html.escape(r['verdict']['reason'])}</td>"
        "</tr>"
    )


def build_report(result: dict, out_path: Path):
    """A dark, screenshot-ready HTML page. The CoinGecko API badge credits the data source."""
    s = result["summary"]
    rows = sorted(result["rows"], key=lambda r: (not r["verdict"]["keep"], r["verdict"]["check"] != "one_hit", -(r["features"].get("lifetime_realized_pnl_usd") or 0)))
    one_hits = [r for r in rows if r["verdict"]["check"] == "one_hit"]
    spotlight = ""
    if one_hits:
        o = max(one_hits, key=lambda r: r["features"].get("lifetime_realized_pnl_usd") or 0)
        b = o["best_token"] or {}
        spotlight = (
            '<div class="spot"><div class="spot-k">LOOKS GOOD, GETS CUT</div>'
            f"<div class=\"spot-v\"><span class=\"mono\">{html.escape(o['short'])}</span> is up "
            f"{_money(o['features'].get('lifetime_realized_pnl_usd'))}, but "
            f"{html.escape(str(b.get('symbol', 'one token')))} alone is "
            f"{round((o['features'].get('profit_concentration') or 0) * 100)}% of that profit.</div></div>"
        )
    ideas_html = ""
    if result.get("ideas"):
        items = "".join(
            f'<div class="idea"><b>{html.escape(str(i["symbol"]))}</b><span>{html.escape(str(i["network"]))} &middot; {i["wallets"]} kept wallets &middot; {_money(i["bought_usd"])} bought</span></div>'
            for i in result["ideas"][:8]
        )
        ideas_html = f'<div class="ideas-h">WHERE THE KEPT WALLETS ARE SITTING</div><div class="ideas">{items}</div>'
    try:
        badge = BRAND_SVG.read_text()
    except OSError:
        badge = "<span>Data: CoinGecko API</span>"
    r_ = result["rules"]
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>Smart Sniper</title>
<style>
:root {{ --bg:#111418; --card:#171b20; --line:#262b31; --text:#e8e6e1; --mute:#8d939a; --keep:#5fc39b; --cut:#e0776b; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Helvetica,Arial,sans-serif; }}
.wrap {{ max-width:1100px; margin:0 auto; padding:40px 36px 32px; }}
.top {{ display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--line); padding-bottom:18px; }}
.top .badge svg {{ height:30px; width:auto; }}
.eyebrow {{ color:var(--mute); font-size:12px; letter-spacing:.14em; text-transform:uppercase; }}
h1 {{ font-size:40px; margin:26px 0 4px; letter-spacing:-.01em; font-weight:650; }}
.sub {{ color:var(--mute); margin:0 0 26px; }}
.stats {{ display:grid; grid-template-columns:repeat(5,1fr); gap:12px; margin-bottom:22px; }}
.stat {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px 18px; }}
.stat b {{ display:block; font-size:30px; font-weight:650; }}
.stat span {{ color:var(--mute); font-size:13px; }}
.stat.k b {{ color:var(--keep); }} .stat.c b {{ color:var(--cut); }}
.spot {{ border:1px solid var(--line); border-left:3px solid var(--cut); background:var(--card); border-radius:8px; padding:14px 18px; margin-bottom:22px; }}
.spot-k {{ color:var(--cut); font-size:12px; letter-spacing:.14em; }}
.spot-v {{ font-size:17px; margin-top:4px; }}
table {{ width:100%; border-collapse:collapse; font-size:14px; }}
th {{ text-align:left; color:var(--mute); font-weight:500; font-size:12px; letter-spacing:.08em; text-transform:uppercase; padding:8px 10px; border-bottom:1px solid var(--line); }}
td {{ padding:9px 10px; border-bottom:1px solid var(--line); }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:13px; }}
.tag {{ font-weight:700; font-size:12px; letter-spacing:.08em; }}
tr.keep .tag {{ color:var(--keep); }} tr.cut .tag {{ color:var(--cut); }} tr.cut td {{ color:#b7bbc0; }}
.ideas-h {{ color:var(--keep); font-size:12px; letter-spacing:.14em; margin:26px 0 10px; }}
.ideas {{ display:grid; grid-template-columns:repeat(4,1fr); gap:10px; }}
.idea {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px 14px; }}
.idea b {{ display:block; font-size:17px; }} .idea span {{ color:var(--mute); font-size:12px; }}
.foot {{ color:var(--mute); font-size:12px; margin-top:22px; display:flex; justify-content:space-between; gap:20px; }}
</style></head><body><div class="wrap">
<div class="top"><div class="eyebrow">Wallet research &middot; {html.escape(core_config.CHAINS.get(result['chain'], result['chain']))} &middot; {html.escape(config.SOURCES.get(result['source'], result['source']))}</div><div class="badge">{badge}</div></div>
<h1>Smart Sniper</h1>
<p class="sub">The top traders behind today's trending pools, checked against their full PnL. Bots and one great trade don't count as a track record.</p>
<div class="stats">
<div class="stat"><b>{len(result['tokens'])}</b><span>trending tokens scanned</span></div>
<div class="stat"><b>{s['wallets_checked']}</b><span>top traders checked</span></div>
<div class="stat c"><b>{s['bots']}</b><span>cut as bots</span></div>
<div class="stat c"><b>{s['one_hit_wonders']}</b><span>cut as one-hit wonders</span></div>
<div class="stat k"><b>{s['kept']}</b><span>kept</span></div>
</div>
{spotlight}
<table><thead><tr><th></th><th>Wallet</th><th class="num">Realized PnL</th><th>Best token</th><th>Why</th></tr></thead>
<tbody>{''.join(_row_html(r) for r in rows[:12])}</tbody></table>
{ideas_html}
<div class="foot"><div>Rules: cut bots (over {r_['max_trades']:,} trades), wallets where one token is over {round(r_['max_profit_concentration'] * 100)}% of profit, fewer than {r_['min_tokens_sold']} closed positions, or win rate under {round(r_['min_win_rate'] * 100)}%.</div>
<div>Pools, traders and PnL from CoinGecko API. Keep/cut verdicts are my own rules. Research only, not financial advice.</div></div>
</div></body></html>"""
    out_path.write_text(doc)


async def main(chain: str, source: str, max_wallets: int, n_tokens: int):
    client = CoinGeckoClient()
    try:
        result = await run(client, chain, source, max_wallets, n_tokens)
    finally:
        await client.close()
    run_dir = Path(config.RUNS_DIR) / f"lucky-{time.strftime('%Y%m%d-%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "results.json").write_text(json.dumps(result, indent=2, default=list))
    build_report(result, run_dir / "report.html")
    print_summary(result, run_dir)
    return result, run_dir
