"""Inventory item lookup, search, and stock reservation."""

from fastapi import APIRouter, Header, HTTPException

from app import cache
from app.models import Page, ReservationRequest
from app.repositories import items as items_repo
from app.repositories import reservations as reservations_repo

router = APIRouter()


def _tenant(x_tenant_id: str = Header()) -> str:
    if not x_tenant_id:
        raise HTTPException(status_code=400, detail="missing tenant")
    return x_tenant_id


@router.get("/items/{sku}")
def get_item(sku: str, x_tenant_id: str = Header()):
    tenant_id = _tenant(x_tenant_id)
    row = items_repo.get_by_sku(sku, tenant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return row


@router.get("/items")
def search_items(
    warehouse_id: str = "",
    q: str = "",
    limit: int = 50,
    cursor: str = "",
    x_tenant_id: str = Header(),
):
    """Search items, newest id last, with keyset pagination by id."""
    tenant_id = _tenant(x_tenant_id)
    # Fetch one extra row to learn whether another page follows; a cursor
    # continues from the last id we returned on the previous page.
    rows = items_repo.search(
        tenant_id,
        limit + 1,
        warehouse_id=warehouse_id,
        q=q,
        cursor=cursor,
    )
    next_cursor = None
    if len(rows) > limit:
        next_cursor = rows[limit]["id"]
        rows = rows[:limit]
    return Page(items=rows, next_cursor=next_cursor)


@router.get("/items/{sku}/stock")
def get_stock(sku: str, warehouse_id: str, x_tenant_id: str = Header()):
    """Return the on-hand quantity for a SKU at a warehouse (cached)."""
    tenant_id = _tenant(x_tenant_id)
    key = cache.stock_key(sku)
    cached = cache.get(key)
    if cached is not None:
        return {"sku": sku, "warehouse_id": warehouse_id, "quantity": cached}
    qty = items_repo.get_quantity(sku, warehouse_id, tenant_id)
    if qty is None:
        raise HTTPException(status_code=404, detail="not found")
    cache.put(key, qty)
    return {"sku": sku, "warehouse_id": warehouse_id, "quantity": qty}


@router.post("/items/reserve")
def reserve_stock(req: ReservationRequest, x_tenant_id: str = Header()):
    """Reserve stock for an order, decrementing on-hand quantity.

    Reservations are idempotent per order_id: a repeated call for an order we
    already reserved is a no-op.
    """
    tenant_id = _tenant(x_tenant_id)

    if reservations_repo.exists(req.order_id, tenant_id):
        return {"order_id": req.order_id, "status": "already_reserved"}

    on_hand = items_repo.get_quantity(req.sku, req.warehouse_id, tenant_id)
    if on_hand is None:
        raise HTTPException(status_code=404, detail="not found")
    if on_hand < req.quantity:
        raise HTTPException(status_code=409, detail="insufficient stock")

    items_repo.decrement_quantity(req.sku, req.warehouse_id, tenant_id, req.quantity)
    reservations_repo.create(
        req.order_id, tenant_id, req.sku, req.warehouse_id, req.quantity
    )
    cache.invalidate(cache.stock_key(req.sku))
    return {"order_id": req.order_id, "status": "reserved"}
