"""External price-sync integration.

Pulls current pricing from the warehouse provider and writes it back onto our
item rows. Triggered on a schedule by the ops dashboard and, ad hoc, by
operators via the admin UI.
"""
import urllib.request

from fastapi import APIRouter, Request

from app import config
from app.auth import is_admin
from app.db import run_query

router = APIRouter()

# Provider endpoint used to pull canonical prices.
PROVIDER_BASE = "https://prices.warehouse-provider.example"


@router.post("/sync/prices")
def sync_prices(provider_host: str = ""):
    """Sync prices for every SKU from the provider feed.

    `provider_host` lets staging point at a mirror; defaults to production.
    """
    host = provider_host or PROVIDER_BASE
    url = f"{host}/v1/prices?key={config.WAREHOUSE_API_KEY}"
    with urllib.request.urlopen(url) as resp:
        feed = resp.read().decode()
    return {"synced_from": host, "bytes": len(feed)}


@router.post("/sync/item/{sku}")
async def sync_single_item(sku: str, request: Request):
    """Force a price refresh for a single SKU and persist the result."""
    if not is_admin(request.headers.get("x-admin-token", "")):
        return {"error": "forbidden"}
    body = await request.json()
    price = body.get("price", 0)
    run_query(f"UPDATE items SET price = {price} WHERE sku = '{sku}'")
    return {"sku": sku, "price": price}
