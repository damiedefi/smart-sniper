"""The chain picker's catalog: every onchain network CoinGecko supports, with logos, cached for 24h.

`GET /onchain/networks` is paginated (100 per page, 250+ networks), so this walks every page. Logos
come from `/asset_platforms` via each network's `coingecko_asset_platform_id`. The result is kept in
memory and in data/chains.json, so a restart doesn't spend credits on it again the same day.
"""
import json
import time
from pathlib import Path

from core import config as core_config
from core.client import CoinGeckoClient, CoinGeckoError

CACHE_PATH = Path("data/chains.json")
CACHE_TTL_S = 24 * 3600
MAX_PAGES = 50

_mem: dict = {"ts": 0.0, "chains": None}


def order_chains(chains: list[dict], popular: list[str] = core_config.POPULAR_CHAINS) -> list[dict]:
    """Popular chains first (in `popular` order, flagged popular=True), then the rest A-Z by name."""
    rank = {cid: i for i, cid in enumerate(popular)}
    for c in chains:
        c["popular"] = c["id"] in rank
    pinned = sorted((c for c in chains if c["popular"]), key=lambda c: rank[c["id"]])
    rest = sorted((c for c in chains if not c["popular"]), key=lambda c: (c.get("name") or c["id"]).lower())
    return pinned + rest


def build_catalog(networks: list[dict], platforms: list[dict]) -> list[dict]:
    """Raw /onchain/networks rows + /asset_platforms rows -> [{id, name, image, platform, popular}]."""
    images = {p.get("id"): ((p.get("image") or {}).get("small") or (p.get("image") or {}).get("thumb")) for p in platforms or []}
    out, seen = [], set()
    for n in networks:
        nid = n.get("id")
        if not nid or nid in seen:
            continue
        seen.add(nid)
        attrs = n.get("attributes") or {}
        platform = attrs.get("coingecko_asset_platform_id")
        image = images.get(platform)
        out.append({"id": nid, "name": attrs.get("name") or nid, "platform": platform, "image": image if image and "missing" not in image else None})
    return order_chains(out)


def _read_disk() -> list[dict] | None:
    try:
        blob = json.loads(CACHE_PATH.read_text())
    except (OSError, ValueError):
        return None
    if time.time() - blob.get("ts", 0) > CACHE_TTL_S:
        return None
    return blob.get("chains")


async def catalog(client: CoinGeckoClient) -> list[dict]:
    """Every network, cached 24h in memory and on disk. Falls back to the known chain labels offline."""
    if _mem["chains"] and time.time() - _mem["ts"] < CACHE_TTL_S:
        return _mem["chains"]
    disk = _read_disk()
    if disk:
        _mem.update(ts=time.time(), chains=disk)
        return disk
    networks: list[dict] = []
    try:
        for page in range(1, MAX_PAGES + 1):
            d = await client.networks_page(page)
            networks += d.get("data", []) or []
            if not (d.get("links") or {}).get("next") or not d.get("data"):
                break
        try:
            platforms = await client.asset_platforms()
        except CoinGeckoError:
            platforms = []
    except CoinGeckoError:
        networks = []
    if not networks:
        return order_chains([{"id": k, "name": v, "image": None, "platform": None} for k, v in core_config.CHAINS.items()])
    chains = build_catalog(networks, platforms)
    _mem.update(ts=time.time(), chains=chains)
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps({"ts": time.time(), "chains": chains}))
    except OSError:
        pass
    return chains


def label(chain_id: str) -> str:
    """Human name for a chain id, from the cached catalog (or the id itself)."""
    for c in _mem.get("chains") or []:
        if c["id"] == chain_id:
            return c["name"]
    return core_config.CHAINS.get(chain_id, chain_id)
