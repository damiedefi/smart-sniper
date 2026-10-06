"""Refresh the hosted demo's data: `python -m app.snapshot_job`

Pulls a fresh Smart Sniper dataset from CoinGecko API and writes docs/data.json, which the GitHub
Pages demo loads. The GitHub Action in .github/workflows/refresh-data.yml runs this every hour with
the API key stored in the repo's encrypted Secrets, so the key never appears in the page or the code.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from core.client import CoinGeckoClient

from . import sniper

OUT = Path(__file__).resolve().parent.parent / "docs" / "data.json"


async def main() -> int:
    if not os.environ.get("COINGECKO_API_KEY"):
        print("COINGECKO_API_KEY is not set. Add it to .env locally, or as a repo Secret for the GitHub Action.")
        return 1
    chain = os.environ.get("SNIPER_CHAIN", "robinhood")
    source = os.environ.get("SNIPER_SOURCE", "trending_24h")
    wallets = int(os.environ.get("SNIPER_WALLETS", "150"))
    client = CoinGeckoClient()
    try:
        data = await sniper.build(client, chain, source, max_wallets=wallets)
    finally:
        await client.close()
    if len(data["wallets"]) < 10:
        print(f"Only {len(data['wallets'])} wallets came back, keeping the previous data.json.")
        return 1
    data["live"] = False
    data["auto"] = True
    OUT.write_text(json.dumps(data, separators=(",", ":")))
    print(f"Wrote {OUT} with {len(data['wallets'])} wallets ({data.get('credits_used')} API calls).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
