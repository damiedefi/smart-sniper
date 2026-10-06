"""Smart Sniper web app: `make sniper`, then open http://localhost:8001

  /                  the interactive UI (web/sniper.html)
  /api/snapshot      live data from CoinGecko API, cached for a few minutes
                     ?chain=robinhood&source=trending_24h&wallets=150&refresh=1

Your API key stays on your machine in .env. The browser only ever talks to this server.
"""
import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse

from core.client import CoinGeckoClient, CoinGeckoError, PlanRestrictedError

from . import config, sniper

WEB = Path(__file__).resolve().parent.parent / "web"
CACHE_S = 300
_cache: dict = {}
_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = CoinGeckoClient()
    yield
    await app.state.client.close()


app = FastAPI(title="Smart Sniper", lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(WEB / "sniper.html")


@app.get("/api/snapshot")
async def snapshot(
    chain: str = Query("robinhood", pattern=r"^[a-z0-9][a-z0-9_-]{0,60}$"),
    source: str = Query("trending_24h"),
    wallets: int = Query(150, ge=20, le=300),
    refresh: int = 0,
):
    if source not in config.SOURCES:
        return JSONResponse({"error": f"Unknown source. Use one of: {', '.join(config.SOURCES)}"}, status_code=400)
    key = (chain, source, wallets)
    async with _lock:
        hit = _cache.get(key)
        if hit and not refresh and time.time() - hit[0] < CACHE_S:
            return hit[1]
        try:
            data = await sniper.build(app.state.client, chain, source, max_wallets=wallets)
        except PlanRestrictedError:
            return JSONResponse({"error": "Wallet data needs a CoinGecko Analyst plan key or above. See coingecko.com/en/api/pricing"}, status_code=402)
        except CoinGeckoError as e:
            return JSONResponse({"error": f"CoinGecko API error: {e}"}, status_code=502)
        _cache[key] = (time.time(), data)
        return data
