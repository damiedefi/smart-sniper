"""Radar-specific settings, layered on top of core.config."""
from core import config as core_config

# The chain the app opens on. The picker lists every network from GET /onchain/networks (app/chains.py).
DEFAULT_CHAIN = "robinhood"

SOURCES = {
    "trending_1h": "Trending (1h)",
    "trending_24h": "Trending (24h)",
    "new_pools": "New pools",
    "safe_movers": "Safe movers",
}

DEFAULT_TOP_N_TOKENS = 10
DEFAULT_WALLETS_PROFILED = 30
DEFAULT_BUDGET_USD = 100
DEFAULT_FOLLOW_POLL_S = 30
DEFAULT_RESCAN_HOURS = 6
DEFAULT_MAX_CREDITS_PER_DAY = 20_000
DEFAULT_TOP_K_FOLLOW = 5

# Safe-movers megafilter preset (PLAN.md §2 / BRIEFS.md).
SAFE_MOVERS_FILTERS = {
    "checks": "no_honeypot,good_gt_score",
    "min_reserve_in_usd": 20_000,
    "min_24h_volume_usd": 20_000,
    "sort": "h24_volume_usd_desc",
    "page": 1,
}

RUNS_DIR = "runs"

REPO_NAME = "smart-money-radar"
BASE_URL_UTM = f"utm_source=github&utm_content={REPO_NAME}"
