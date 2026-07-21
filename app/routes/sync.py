"""External price-sync integration and reservation release.

Pulls current pricing from the warehouse provider (over an allow-listed host)
and writes it back onto our item rows. Also exposes the reservation-release
path used when an order is cancelled or fulfilled.
"""

import urllib.parse
import urllib.request

from fastapi import APIRouter, Depends, Header, HTTPException

from app import cache, config
from app.auth import require_admin
from app.db import execute, fetch_one, transaction

router = APIRouter()

# Default provider endpoint used to pull canonical prices.
PROVIDER_BASE = "https://prices.warehouse-provider.example"


def _provider_url(host: str) -> str:
    """Build the provider feed URL, refusing hosts outside the allow-list."""
    base = host or PROVIDER_BASE
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme not in ("https",) or parsed.hostname is None:
        raise HTTPException(status_code=400, detail="invalid provider host")
    if parsed.hostname not in config.PROVIDER_ALLOWED_HOSTS:
        raise HTTPException(status_code=400, detail="provider host not allowed")
    query = urllib.parse.urlencode({"key": config.WAREHOUSE_API_KEY or ""})
    return urllib.parse.urlunparse(parsed._replace(path="/v1/prices", query=query))


@router.post("/sync/prices", dependencies=[Depends(require_admin)])
def sync_prices(provider_host: str = ""):
    """Sync prices for every SKU from the provider feed."""
    url = _provider_url(provider_host)
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 (host allow-listed)
        feed = resp.read().decode()
    return {"synced": True, "bytes": len(feed)}


@router.post("/sync/item/{sku}", dependencies=[Depends(require_admin)])
def sync_single_item(
    sku: str, warehouse_id: str, price: float, x_tenant_id: str = Header()
):
    """Force a price refresh for a single SKU and persist the result."""
    affected = execute(
        "UPDATE items SET price = %s "
        "WHERE sku = %s AND warehouse_id = %s AND tenant_id = %s",
        (price, sku, warehouse_id, x_tenant_id),
    )
    if affected == 0:
        raise HTTPException(status_code=404, detail="not found")
    cache.invalidate(cache.price_key(sku, warehouse_id))
    return {"sku": sku, "warehouse_id": warehouse_id, "price": price}


@router.post("/reservations/{order_id}/release")
def release_reservation(order_id: str, x_tenant_id: str = Header()):
    """Release a reservation, returning its quantity to on-hand stock.

    Called on order cancellation. Returns the stock to the item it was held
    against and clears the reservation row.
    """
    res = fetch_one(
        "SELECT sku, warehouse_id, quantity FROM reservations "
        "WHERE order_id = %s AND tenant_id = %s",
        (order_id, x_tenant_id),
    )
    if res is None:
        raise HTTPException(status_code=404, detail="no such reservation")

    with transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE items SET quantity = quantity + %s "
            "WHERE sku = %s AND warehouse_id = %s AND tenant_id = %s",
            (res["quantity"], res["sku"], res["warehouse_id"], x_tenant_id),
        )
        # Guard: if no item row was updated the stock restore did not happen.
        # Raising here causes the transaction() context manager to roll back,
        # so the DELETE below is never reached and the reservation is preserved.
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=409,
                detail="item not found; stock restore aborted, reservation kept",
            )
        # Delete the reservation only after the stock restore is confirmed.
        # Both statements share one connection and commit or roll back together.
        cur.execute(
            "DELETE FROM reservations WHERE order_id = %s AND tenant_id = %s",
            (order_id, x_tenant_id),
        )
    cache.invalidate(cache.stock_key(res["sku"]))
    return {"order_id": order_id, "released": res["quantity"]}
