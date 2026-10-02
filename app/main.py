"""donut.auction live price API.

Self-hosted read-only bridge: scrapes the public JSON API behind
https://donut.auction and exposes it as a simple queryable HTTP API.

    GET /price?query=netherite_ingot
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from .donut import PERIODS, DonutClient, UpstreamError, normalize_query, prettify

TTL = float(os.getenv("CACHE_TTL_SECONDS", "15"))
client = DonutClient(ttl=TTL)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await client.startup()
    try:
        yield
    finally:
        await client.shutdown()


app = FastAPI(
    title="donut.auction price API",
    version="1.0.0",
    description="Live DonutSMP auction-house prices as JSON.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _source() -> dict[str, Any]:
    return {
        "site": "https://donut.auction",
        "upstream": client.base,
        "fetchedAt": _now(),
        "cacheTtlSeconds": TTL,
    }


async def _resolve_one(query: str) -> dict[str, Any]:
    ranked = await client.resolve(query)
    if not ranked:
        raise HTTPException(status_code=404, detail=f"no item matched '{query}'")
    return ranked[0]


def _price_block(match: dict[str, Any]) -> dict[str, Any]:
    price = match.get("price") or {}
    value = price.get("value")
    return {
        "value": round(float(value), 4) if value is not None else None,
        "unit": "donut_coins",
        "volume24h": price.get("volume24Hours"),
        "sales24h": price.get("saleCount24Hours"),
    }


def _trend_summary(history: dict[str, Any]) -> dict[str, Any] | None:
    points = [p for p in history.get("dataPoints", []) if p.get("avgPrice")]
    if not points:
        return None
    prices = [float(p["avgPrice"]) for p in points]
    first, last = prices[0], prices[-1]
    return {
        "period": history.get("period"),
        "interval": history.get("interval"),
        "points": len(points),
        "first": round(first, 2),
        "last": round(last, 2),
        "low": round(min(prices), 2),
        "high": round(max(prices), 2),
        "changePercent": round((last - first) / first * 100, 3) if first else None,
    }


async def _build_price(query: str, period: str | None) -> dict[str, Any]:
    match = await _resolve_one(query)
    item_id = match["itemId"]
    listing, sales = await _gather_listing_and_sales(item_id)

    payload: dict[str, Any] = {
        "query": query,
        "found": True,
        "item": {
            "itemId": item_id,
            "itemName": match["itemName"],
            "displayName": match["displayName"],
            "enchantments": match["enchantments"],
        },
        "price": _price_block(match),
        "bestListing": listing,
        "lastSale": sales,
        "source": _source(),
    }
    if period:
        history = await client.history(item_id, period)
        payload["trend"] = _trend_summary(history)
        payload["trendPeriod"] = period
    return payload


async def _gather_listing_and_sales(item_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        listing = await client.best_listing(item_id)
    except UpstreamError:
        listing = None
    try:
        sales = await client.transactions(item_id)
    except UpstreamError:
        sales = []
    last_sale = None
    if sales:
        newest = sales[0]
        last_sale = {
            "price": newest.get("price"),
            "itemCount": newest.get("itemCount"),
            "seller": (newest.get("seller") or {}).get("name"),
            "at": newest.get("timeSold"),
        }
    return listing, last_sale


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> str:
    return """<!doctype html><meta charset="utf-8">
<title>donut.auction price API</title>
<style>
 body{font:15px/1.6 system-ui,sans-serif;max-width:760px;margin:3rem auto;padding:0 1rem;background:#111;color:#eee}
 code{background:#222;padding:.15em .4em;border-radius:4px}
 a{color:#7cc4ff} li{margin:.35rem 0}
</style>
<h1>donut.auction price API</h1>
<p>Live DonutSMP auction-house prices, scraped from <a href="https://donut.auction">donut.auction</a>.</p>
<ul>
 <li><a href="/price?query=netherite_ingot"><code>/price?query=netherite_ingot</code></a> &mdash; one item: market value, cheapest live listing, last sale</li>
 <li><code>/price?query=elytra&amp;period=7d</code> &mdash; adds a trend summary (period: 1d, 7d, 30d, all)</li>
 <li><code>/prices?query=netherite&amp;limit=10</code> &mdash; every match for a partial name</li>
 <li><code>/history?query=netherite_ingot&amp;period=7d</code> &mdash; raw price history buckets</li>
 <li><code>/transactions?query=netherite_ingot&amp;limit=25</code> &mdash; recent completed sales</li>
 <li><code>/listings?query=netherite_ingot</code> &mdash; live listings for one item</li>
 <li><code>/item/{itemId}</code> &mdash; item metadata by UUID</li>
 <li><code>/health</code> &middot; <a href="/docs">/docs</a></li>
</ul>"""


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "upstream": client.base, "time": _now()}


@app.get("/price")
async def price(
    query: str = Query(..., description="Item name, e.g. netherite_ingot"),
    period: str | None = Query(None, description="Optional trend period: 1d, 7d, 30d, all"),
) -> dict[str, Any]:
    if period and period not in PERIODS:
        raise HTTPException(status_code=400, detail=f"period must be one of {sorted(PERIODS)}")
    try:
        return await _build_price(query, period)
    except UpstreamError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.get("/prices")
async def prices(
    query: str = Query(..., description="Full or partial item name"),
    limit: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    try:
        ranked = await client.resolve(query)
    except UpstreamError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {
        "query": query,
        "count": len(ranked),
        "results": [
            {
                "itemId": m["itemId"],
                "itemName": m["itemName"],
                "displayName": m["displayName"],
                "enchantments": m["enchantments"],
                "price": _price_block(m),
            }
            for m in ranked[:limit]
        ],
        "source": _source(),
    }


@app.get("/history")
async def history(
    query: str = Query(...),
    period: str = Query("7d", description="1d, 7d, 30d or all"),
) -> dict[str, Any]:
    match = await _resolve_one(query)
    data = await client.history(match["itemId"], period)
    return {
        "query": query,
        "item": {"itemId": match["itemId"], "itemName": match["itemName"]},
        "trend": _trend_summary(data),
        "period": data.get("period"),
        "interval": data.get("interval"),
        "dataPoints": data.get("dataPoints", []),
        "source": _source(),
    }


@app.get("/transactions")
async def transactions(
    query: str = Query(...),
    limit: int = Query(25, ge=1, le=200),
) -> dict[str, Any]:
    match = await _resolve_one(query)
    sales = await client.transactions(match["itemId"])
    return {
        "query": query,
        "item": {"itemId": match["itemId"], "itemName": match["itemName"]},
        "count": len(sales),
        "transactions": sales[:limit],
        "source": _source(),
    }


@app.get("/listings")
async def listings(query: str = Query(...)) -> dict[str, Any]:
    match = await _resolve_one(query)
    live = [t for t in await client.tickers() if t.get("itemId") == match["itemId"]]
    live.sort(key=lambda t: t.get("unitPrice") or t.get("listingPrice") or float("inf"))
    return {
        "query": query,
        "item": {"itemId": match["itemId"], "itemName": match["itemName"]},
        "count": len(live),
        "listings": live,
        "source": _source(),
    }


@app.get("/item/{item_id}")
async def item(item_id: str) -> dict[str, Any]:
    data = await client.item(item_id)
    if not data.get("id"):
        raise HTTPException(status_code=404, detail="item not found")
    name = data.get("itemName", "")
    return {
        "itemId": data.get("id"),
        "itemName": name,
        "displayName": prettify(name),
        "enchantments": data.get("enchantments") or [],
        "source": _source(),
    }


@app.get("/resolve")
async def resolve(query: str = Query(..., description="Free-text name to normalise")) -> dict[str, Any]:
    """Debug helper: shows how a query string is parsed before matching."""
    return {"query": query, "normalized": normalize_query(query)}
