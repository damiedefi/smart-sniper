# Agent notes for Smart Money Radar

This repo is meant to be forked and reskinned by an AI coding agent. Keep changes small and
readable -- the next agent to touch this (maybe you, in a later session) has to understand it fast.

## Repo map

```
core/           shared CoinGecko client, paper-trading engine, plan detection, report/article-kit
                builder. Don't rewrite this wholesale; it's shared logic ported from a larger kit.
app/
  config.py     chains, sources, defaults, the safe-movers megafilter preset
  scan.py       Scan tab: pools -> tokens -> top_traders -> deduped wallet candidates
  scoring.py    skill_score() + profile_wallet(): the deterministic scoring formula
  profile.py    Wallets tab: live wallet_pnl + wallet_trades + wallet_balances calls
  follow.py     Follow tab / forward test: polls followed wallets, mirrors trades into core.paper
  backtest.py   walk-forward wallet selection + blind vs size-matched paper-copy scenarios
  autopilot.py  rescans on a schedule, keeps the top-K copyable wallets, trades forever
  runs.py       runs/<id>/ directory layout (decisions.jsonl, trades.csv, metrics.json)
  cli.py        `python -m app.cli <backtest|forward|autopilot|report|article-kit|set-link|record|replay>`
  server.py     FastAPI app: serves web/ and the Scan/Wallets/Follow/Runs API
web/            plain HTML/JS/CSS UI. No build step, no framework.
tests/          offline pytest (no network): scan dedupe, scoring, backtest no-lookahead, plan-lock
```

## Run / test commands

```
make install                              # uv venv + editable install
make run                                  # FastAPI dev server on :8000
make test                                 # pytest, no network needed
make backtest SOURCE=trending_1h CHAIN=solana
make forward MINUTES=3
make autopilot
make report RUN=latest
make article-kit RUN=latest HANDLE=you
make set-link HANDLE=you
```

## Customization recipes

- **Reskin**: edit CSS variables in `core/web/theme.css` (colors, radius, fonts) and set
  `data-theme` on `<html>` in `web/index.html` to `light`, `dark`, or `mono`. The CoinGecko badge
  and article-kit assets pick the matching logo automatically from `--theme-mode` -- you never need
  to swap the logo file by hand.
- **Change the strategy / scoring**: `app/scoring.py` has one function, `skill_score()`, and one
  composite label rule in `core/wallets.label_wallet()`. Change the weights, or add a new label.
- **Add a filter to Scan**: `app/scan.py` builds the megafilter params in `app/config.py`
  (`SAFE_MOVERS_FILTERS`). Add keys there; see the onchain pools megafilter docs for the full list.
- **Add a chain**: add it to `WALLET_CHAINS` in `app/config.py` and to `CHAINS` /
  `WALLET_CHAIN_CAPS` in `core/config.py` if the wallet endpoints support it differently there
  (some chains, like Solana, don't support `balances`/`transfers` -- respect `WALLET_CHAIN_CAPS`,
  don't just assume every endpoint works everywhere).
- **Change paper-trading assumptions**: edit `assumptions.yaml` (slippage, fees, budget, max
  position, cooldown). No code change needed; the UI's Assumptions panel reads it live.
- **Add an endpoint**: verify the path and params in the CoinGecko API docs first. `core/client.py`
  is a plain `httpx` wrapper -- add a typed method there following the existing pattern. The
  official SDK (`pip install coingecko-sdk`) also covers most endpoints if you'd rather use that for
  something new.

## Rules

- Keep the CoinGecko badge and the CoinGecko links block in `README.md` (between
  `<!-- coingecko-links:start -->` and `:end -->`). Restyle freely, never remove them.
- Use the right logo variant for the background it sits on: full-color on light, full-color with a
  white wordmark on dark, monochrome where color would clash. Never recolor, stretch, or crop it.
- Never commit `.env`. Keys load from `.env` only, never printed or logged.
- Paper trading only, unless you deliberately wire up a real executor yourself -- that's outside
  this repo's scope by design.
- Before using a new endpoint, confirm its path and params against the CoinGecko API docs (or the
  official SDK's `/reference/{operationId}.md`), don't guess from memory.
- Recommended: install the CoinGecko Agent Skill (`npx skills add coingecko/skills -g -y`) and the
  Docs MCP (`https://docs.coingecko.com/mcp`?utm_source=github&utm_content=damidefi) so your agent already knows this API.
