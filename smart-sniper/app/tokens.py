"""Token symbols + logos for any (network, address), cached so every page can show a logo cheaply.

Wallet trades and positions only carry token addresses. This resolves them with the onchain
`tokens/multi` endpoint (30 per call), keeps the answer for an hour, and never lets a lookup failure
break the page that asked: an unresolved token just gets a short address and an initials avatar.
"""
import asyncio
import time

from core.client import CoinGeckoClient, CoinGeckoError

TTL_S = 3600
MAX_PER_NETWORK = 60  # caps one page render at 2 calls per network
_cache: dict[str, tuple[float, dict]] = {}


def _key(network: str, address: str) -> str:
    return f"{network}:{(address or '').lower()}"


def short(address: str | None) -> str:
    a = address or ""
    return a if len(a) < 12 else f"{a[:4]}…{a[-4:]}"


def is_native_placeholder(address: str | None) -> bool:
    return (address or "").lower().startswith("0xeeeeeeee")


def remember(network: str, address: str, symbol: str | None = None, name: str | None = None, image: str | None = None):
    """Seeds the cache with metadata a response already carried (so we don't look it up again)."""
    if not address:
        return
    k = _key(network, address)
    old = (_cache.get(k) or (0, {}))[1]
    merged = {**old, "symbol": symbol or old.get("symbol"), "name": name or old.get("name"), "image": image or old.get("image")}
    _cache[k] = (time.time(), merged)


def get(network: str, address: str) -> dict:
    """Cached metadata for one token: {symbol, name, image}, or a short-address fallback."""
    hit = _cache.get(_key(network, address))
    meta = hit[1] if hit else {}
    return {"symbol": meta.get("symbol") or short(address), "name": meta.get("name"), "image": meta.get("image")}


async def resolve(client: CoinGeckoClient, pairs: set[tuple[str, str]]) -> dict[str, dict]:
    """Fills the cache for every (network, address) missing a logo, then returns {"net:addr": meta}."""
    now = time.time()
    missing: dict[str, list[str]] = {}
    for net, addr in pairs:
        if not net or not addr or is_native_placeholder(addr):
            continue
        hit = _cache.get(_key(net, addr))
        if hit and hit[1].get("image") and now - hit[0] < TTL_S:
            continue
        if hit and hit[1].get("_tried") and now - hit[0] < TTL_S:
            continue
        missing.setdefault(net, [])
        if len(missing[net]) < MAX_PER_NETWORK and addr not in missing[net]:
            missing[net].append(addr)

    async def fetch(net: str, addrs: list[str]):
        try:
            rows = await client.tokens_multi(net, addrs)
        except CoinGeckoError:
            rows = []
        found = set()
        for a in rows:
            image = a.get("image_url")
            if image in (None, "missing.png") or "missing" in str(image):
                image = None
            _cache[_key(net, a.get("address", ""))] = (time.time(), {"symbol": a.get("symbol"), "name": a.get("name"), "image": image, "_tried": True})
            found.add((a.get("address") or "").lower())
        for addr in addrs:
            if addr.lower() not in found:
                old = (_cache.get(_key(net, addr)) or (0, {}))[1]
                _cache[_key(net, addr)] = (time.time(), {**old, "_tried": True})

    await asyncio.gather(*(fetch(n, a) for n, a in missing.items()))
    return {_key(n, a): get(n, a) for n, a in pairs}
