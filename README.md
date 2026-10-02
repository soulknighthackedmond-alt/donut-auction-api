# donut.auction price API

A small self-hosted scraper that turns [donut.auction](https://donut.auction) into a
queryable HTTP API for live DonutSMP auction-house prices.

```
GET /price?query=netherite_ingot
```

```json
{
  "query": "netherite_ingot",
  "found": true,
  "item": {
    "itemId": "e5bce2cd-c5d1-42c2-9f3a-86f298c6ecff",
    "itemName": "netherite_ingot",
    "displayName": "Netherite Ingot",
    "enchantments": []
  },
  "price": { "value": 4706927.7791, "unit": "donut_coins", "volume24h": 0, "sales24h": 0 },
  "bestListing": {
    "unitPrice": 5500000, "listingPrice": 5500000, "quantity": 1,
    "observedAt": "2026-10-02T14:23:40.661Z", "isStale": false, "listingsTracked": 1
  },
  "lastSale": { "price": 4700000, "itemCount": 1, "seller": "noamgr17", "at": "2026-09-29T09:19:10.147Z" },
  "source": { "site": "https://donut.auction", "fetchedAt": "2026-10-02T14:23:46Z", "cacheTtlSeconds": 15.0 }
}
```

## How it works

donut.auction is a SvelteKit front end whose data comes from a public JSON API at
`https://api.donut.auction`. This service wraps those endpoints, ranks the results for a
human item name, and caches them briefly so a burst of queries never hammers upstream.

Upstream endpoints used:

| Endpoint | Used for |
| --- | --- |
| `GET /v2/items/search?q=` | name → item + market value (the main lookup) |
| `GET /v2/tickers/` | live listings snapshot (cheapest current listing) |
| `GET /v2/auctions/items/{id}/transactions` | most recent completed sale |
| `GET /v2/auctions/items/{id}/prices?period=` | price history / trend (`1d`, `7d`, `30d`, `all`) |
| `GET /v2/items/{id}` | item metadata by UUID |

If upstream is unreachable or errors, the last good cached response is served instead of
failing; the cache TTL is `CACHE_TTL_SECONDS` (default 15).

## Endpoints

| Route | What you get |
| --- | --- |
| `GET /price?query=netherite_ingot` | market value + cheapest live listing + last sale |
| `GET /price?query=elytra&period=7d` | the same, plus a trend summary |
| `GET /prices?query=netherite&limit=10` | every item matching a partial name, ranked |
| `GET /history?query=netherite_ingot&period=7d` | raw price history buckets |
| `GET /transactions?query=netherite_ingot&limit=25` | recent completed sales |
| `GET /listings?query=netherite_ingot` | live listings for one item |
| `GET /item/{itemId}` | item metadata by UUID |
| `GET /resolve?query=Netherite Ingot` | debug: how a query is normalised |
| `GET /health`, `GET /docs` | health check, OpenAPI docs |

`query` accepts sloppy input — `netherite_ingot`, `Netherite Ingot`, `minecraft:netherite_ingot`
and `netherite-ingot` all resolve to the same item. Unknown items return `404`, a bad
`period` returns `400`, an upstream failure returns `502`.

## Run it

Local (Python 3.12+):

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8787
```

Docker / Compose:

```bash
docker compose up -d --build
curl "http://localhost:8787/price?query=netherite_ingot"
```

Smoke test (hits the real upstream, so it needs network):

```bash
python test_smoke.py http://localhost:8787
```

## Deploying to 192.168.50.60

The Coolify panel on `192.168.50.60` builds this straight from the repo. Either:

- **Compose resource** — point Coolify at this repo, Docker Compose build pack, port `8787`.
- **Dockerfile application** — build pack Dockerfile, port `8787`.

Once deployed the API is at `http://192.168.50.60:8787/price?query=netherite_ingot`.

Environment variables:

| Var | Default | Meaning |
| --- | --- | --- |
| `CACHE_TTL_SECONDS` | `15` | how long a query result is reused |
| `DONUT_API_BASE` | `https://api.donut.auction` | upstream base URL |
| `PORT` | `8787` | port uvicorn binds |

## Notes

- Read-only: it only ever GETs from donut.auction, and it needs no API key.
- The upstream API is undocumented and unofficial — if donut.auction changes its
  endpoints, `app/donut.py` is the single place to update.
- Prices are in DonutSMP coins, as reported by the site.
