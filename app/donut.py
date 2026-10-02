"""donut.auction upstream client.

Wraps the public JSON API that powers https://donut.auction (api.donut.auction)
and adds a small TTL cache so a burst of queries does not hammer them.

Endpoints used (discovered from the site's client bundle):
  GET /v2/items/search?q=<name>                        -> item + market value
  GET /v2/tickers/                                     -> live listings snapshot
  GET /v2/items/{itemId}                               -> item metadata
  GET /v2/auctions/items/{itemId}/prices?period=       -> price history (1d/7d/30d/all)
  GET /v2/auctions/items/{itemId}/transactions         -> recent sales
"""

from __future__ import annotations

import asyncio
import difflib
import os
import re
import time
from typing import Any

import httpx

UPSTREAM = os.getenv("DONUT_API_BASE", "https://api.donut.auction").rstrip("/")
USER_AGENT = os.getenv(
    "DONUT_USER_AGENT",
    "donut-auction-api/1.0 (+local self-hosted price bridge)",
)

PERIODS = {"1d", "7d", "30d", "all"}
ENCHANT_PREFIX = re.compile(r"^\[[^\]]*\]\s*")


def prettify(item_name: str) -> str:
    """netherite_ingot -> Netherite Ingot"""
    return " ".join(w.capitalize() for w in item_name.replace("-", "_").split("_") if w)


def normalize_query(raw: str) -> str:
    """Accept 'Netherite Ingot', 'minecraft:netherite_ingot', 'netherite-ingot'."""
    q = ENCHANT_PREFIX.sub("", (raw or "").strip())
    q = q.replace("minecraft:", "").replace(" ", "_").replace("-", "_")
    return re.sub(r"_+", "_", q).strip("_").lower()


class UpstreamError(RuntimeError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


class _TTLCache:
    def __init__(self, ttl: float):
        self.ttl = ttl
        self._data: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def peek(self, key: str) -> tuple[Any | None, bool]:
        """Return (value, fresh). Expired values are still returned for stale fallback."""
        entry = self._data.get(key)
        if not entry:
            return None, False
        expiry, value = entry
        return value, expiry > time.monotonic()

    def store(self, key: str, value: Any) -> None:
        self._data[key] = (time.monotonic() + self.ttl, value)

    def lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())


class DonutClient:
    def __init__(self, base: str = UPSTREAM, ttl: float = 15.0):
        self.base = base
        self.cache = _TTLCache(ttl)
        self._client: httpx.AsyncClient | None = None

    async def startup(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self.base,
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )

    async def shutdown(self) -> None:
        if self._client:
            await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Cached GET. Serves a stale copy if upstream fails and we have one."""
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        value, fresh = self.cache.peek(key)
        if fresh:
            return value
        async with self.cache.lock(key):
            value, fresh = self.cache.peek(key)
            if fresh:
                return value
            assert self._client is not None, "client not started"
            try:
                r = await self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                if value is not None:
                    return value
                raise UpstreamError(f"donut.auction unreachable: {exc}") from exc
            if r.status_code >= 400:
                if value is not None:
                    return value
                raise UpstreamError(
                    f"donut.auction returned {r.status_code} for {path}", status=502
                )
            try:
                data = r.json()
            except ValueError as exc:
                if value is not None:
                    return value
                raise UpstreamError("donut.auction returned non-JSON") from exc
            self.cache.store(key, data)
            return data

    # ---- raw endpoints -------------------------------------------------
    async def search_items(self, query: str) -> list[dict[str, Any]]:
        data = await self._get("/v2/items/search", {"q": query})
        items = (data or {}).get("items", []) if isinstance(data, dict) else []
        return [i for i in items if i.get("item")]

    async def tickers(self) -> list[dict[str, Any]]:
        data = await self._get("/v2/tickers/")
        return data if isinstance(data, list) else []

    async def item(self, item_id: str) -> dict[str, Any]:
        data = await self._get(f"/v2/items/{item_id}")
        return data if isinstance(data, dict) else {}

    async def history(self, item_id: str, period: str = "7d") -> dict[str, Any]:
        period = period if period in PERIODS else "7d"
        data = await self._get(f"/v2/auctions/items/{item_id}/prices", {"period": period})
        return data if isinstance(data, dict) else {}

    async def transactions(self, item_id: str) -> list[dict[str, Any]]:
        data = await self._get(f"/v2/auctions/items/{item_id}/transactions")
        return (data or {}).get("transactions", []) if isinstance(data, dict) else []

    # ---- composed lookups ----------------------------------------------
    @staticmethod
    def _score(query: str, item_name: str) -> float:
        if item_name == query:
            return 100.0
        if item_name.startswith(query):
            return 80.0
        if query in item_name:
            return 60.0
        ratio = difflib.SequenceMatcher(None, query, item_name).ratio()
        return 50.0 * ratio

    async def resolve(self, query: str) -> list[dict[str, Any]]:
        """Search upstream and rank the results, best match first."""
        q = normalize_query(query)
        if not q:
            return []
        matches = await self.search_items(q)
        ranked = []
        for entry in matches:
            item = entry["item"]
            name = (item.get("itemName") or "").lower()
            price = entry.get("price") or {}
            ranked.append(
                {
                    "itemId": item.get("id"),
                    "itemName": name,
                    "displayName": prettify(name),
                    "enchantments": item.get("enchantments") or [],
                    "price": price,
                    "_score": self._score(q, name),
                }
            )
        # the site itself hides zero-value items; keep them last instead of dropping
        ranked.sort(key=lambda m: (m["_score"], float(m["price"].get("value") or 0)), reverse=True)
        return ranked

    async def best_listing(self, item_id: str) -> dict[str, Any] | None:
        """Cheapest live auction listing currently observed for this item."""
        live = [t for t in await self.tickers() if t.get("itemId") == item_id]
        if not live:
            return None
        live.sort(key=lambda t: t.get("unitPrice") or t.get("listingPrice") or float("inf"))
        best = live[0]
        return {
            "unitPrice": best.get("unitPrice"),
            "listingPrice": best.get("listingPrice"),
            "quantity": best.get("quantity"),
            "enchantments": best.get("enchantments") or [],
            "observedAt": best.get("observedAt"),
            "isStale": best.get("isStale"),
            "listingsTracked": len(live),
        }
