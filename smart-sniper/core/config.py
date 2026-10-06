"""Loads .env and holds every tunable in one place."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

API_KEY = os.environ.get("COINGECKO_API_KEY", "")
ENVIRONMENT = os.environ.get("COINGECKO_ENVIRONMENT", "pro").strip().lower()

if ENVIRONMENT == "demo":
    BASE_URL = "https://api.coingecko.com/api/v3"
    KEY_HEADER = "x-cg-demo-api-key"
else:
    BASE_URL = "https://pro-api.coingecko.com/api/v3"
    KEY_HEADER = "x-cg-pro-api-key"

WS_URL = "wss://stream.coingecko.com/v1?x_cg_pro_api_key={key}"
PRICING_URL = "https://www.coingecko.com/en/api/pricing"
API_URL = "https://www.coingecko.com/en/api"
DOCS_URL = "https://docs.coingecko.com"

# Onchain chains the repos know how to label. Add more here to support them in the UI.
CHAINS = {
    "solana": "Solana",
    "base": "Base",
    "eth": "Ethereum",
    "bsc": "BNB Chain",
    "polygon_pos": "Polygon",
    "arbitrum": "Arbitrum",
    "optimism": "Optimism",
    "avax": "Avalanche",
    "robinhood": "Robinhood Chain",
}

# Wallet endpoints don't cover every chain the same way. Configure it per chain here;
# the UI hides a tab rather than showing broken or empty data for an unsupported chain.
WALLET_CHAINS = list(CHAINS.keys())
_EVM_CAPS = {"trades": True, "pnl": True, "balances": True, "transfers": True}
WALLET_CHAIN_CAPS = {
    "solana": {"trades": True, "pnl": True, "balances": False, "transfers": False},
}


# The multi-chain wallet endpoints (pnl, balances) take a `networks` list from one VM family per call.
# This is the default EVM scope a wallet lookup asks for; the chain you're viewing is always added on
# top, and networks the endpoint doesn't cover are simply ignored or answered with an error the UI shows.
WALLET_EVM_NETWORKS = ["eth", "base", "bsc", "polygon_pos", "arbitrum", "optimism", "avax", "stable", "robinhood"]

# Chains pinned to the top of the chain picker, in this order. Every other network follows A-Z.
POPULAR_CHAINS = ["solana", "eth", "base", "bsc", "arbitrum", "polygon_pos", "robinhood", "optimism", "avax", "hyperevm", "sui-network", "ton", "tron", "unichain", "sonic", "abstract"]


def wallet_caps(chain: str) -> dict:
    """What the wallet endpoints support for this chain."""
    return WALLET_CHAIN_CAPS.get(chain, _EVM_CAPS)


# Tunables
CONCURRENCY = 10
DEFAULT_TTL_S = 30
TRENDING_TTL_S = 30
INFO_TTL_S = 600
STABLE_TTL_S = 24 * 3600
MAX_RETRIES = 4
BACKOFF_BASE_S = 1.5
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 10
