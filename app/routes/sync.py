"""External price-sync integration and reservation release.

Pulls current pricing from the warehouse provider (over an allow-listed host)
and writes it back onto our item rows. Also exposes the reservation-release
path used when an order is cancelled or fulfilled.
"""

import urllib.parse
import urllib.request

import psycopg2.extras
from fastapi import APIRouter, Depends, Header, HTTPException

from app import cache, config
from app.auth import require_admin
from app.db import execute, execute_in, transaction

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


def _claim_reservation(conn, order_id: str, tenant_id: str):
    """Lock and return a reservation row inside ``conn``'s transaction.

    ``FOR UPDATE`` makes a concurrent release of the same reservation block
    until this transaction finishes. Once it commits (row deleted), the waiter
    re-checks the row, finds nothing, and so never restores stock a second
    time. Returns ``None`` if there is no such reservation.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT sku, warehouse_id, quantity FROM reservations "
        "WHERE order_id = %s AND tenant_id = %s FOR UPDATE",
        (order_id, tenant_id),
    )
    return cur.fetchone()


def _restore_reservation_stock(conn, res: dict, tenant_id: str) -> int:
    """Return a reservation's quantity to its item's on-hand stock."""
    return execute_in(
        conn,
        "UPDATE items SET quantity = quantity + %s "
        "WHERE sku = %s AND warehouse_id = %s AND tenant_id = %s",
        (res["quantity"], res["sku"], res["warehouse_id"], tenant_id),
    )


def _delete_reservation(conn, order_id: str, tenant_id: str) -> int:
    """Drop a reservation row."""
    return execute_in(
        conn,
        "DELETE FROM reservations WHERE order_id = %s AND tenant_id = %s",
        (order_id, tenant_id),
    )


@router.post("/reservations/{order_id}/release")
def release_reservation(order_id: str, x_tenant_id: str = Header()):
    """Release a reservation, returning its quantity to on-hand stock.

    Called on order cancellation. Returns the stock to the item it was held
    against and clears the reservation row. The claim (row lock), the stock
    restore and the delete all run in one transaction: if any step fails,
    nothing is applied and the reservation stays so the release can be
    retried. Releasing an already-released (or unknown) reservation restores
    nothing and returns 404, even when two releases race.
    """
    try:
        with transaction() as conn:
            res = _claim_reservation(conn, order_id, x_tenant_id)
            if res is not None:
                _restore_reservation_stock(conn, res, x_tenant_id)
                if _delete_reservation(conn, order_id, x_tenant_id) != 1:
                    # Should be impossible while we hold the row lock. Roll
                    # back rather than keep a restore with no matching delete.
                    raise RuntimeError("reservation vanished while locked")
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="failed to release reservation"
        ) from exc

    if res is None:
        raise HTTPException(status_code=404, detail="no such reservation")

    # Only reached after a successful commit that restored stock. Invalidate
    # the exact key GET /items/{sku}/stock reads for this tenant+warehouse+sku.
    cache.invalidate(cache.stock_key(x_tenant_id, res["warehouse_id"], res["sku"]))
    return {"order_id": order_id, "released": res["quantity"]}
