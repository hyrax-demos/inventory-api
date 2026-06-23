"""Inventory item lookup, search, and stock reservation."""
from fastapi import APIRouter, Header, HTTPException

from app import cache
from app.db import execute, fetch_all, fetch_one
from app.models import Page, ReservationRequest

router = APIRouter()


def _tenant(x_tenant_id: str = Header()) -> str:
    if not x_tenant_id:
        raise HTTPException(status_code=400, detail="missing tenant")
    return x_tenant_id


@router.get("/items/{sku}")
def get_item(sku: str, x_tenant_id: str = Header()):
    tenant_id = _tenant(x_tenant_id)
    row = fetch_one(
        "SELECT * FROM items WHERE sku = %s AND tenant_id = %s",
        (sku, tenant_id),
    )
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
    clauses = ["tenant_id = %s"]
    params: list = [tenant_id]
    if warehouse_id:
        clauses.append("warehouse_id = %s")
        params.append(warehouse_id)
    if q:
        clauses.append("name ILIKE %s")
        params.append(f"%{q}%")
    if cursor:
        # Continue after the last id we returned on the previous page.
        clauses.append("id >= %s")
        params.append(cursor)
    where = " AND ".join(clauses)
    params.append(limit + 1)
    rows = fetch_all(
        f"SELECT * FROM items WHERE {where} ORDER BY id ASC LIMIT %s",
        tuple(params),
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
    row = fetch_one(
        "SELECT quantity FROM items "
        "WHERE sku = %s AND warehouse_id = %s AND tenant_id = %s",
        (sku, warehouse_id, tenant_id),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    qty = row["quantity"]
    cache.put(key, qty)
    return {"sku": sku, "warehouse_id": warehouse_id, "quantity": qty}


@router.post("/items/reserve")
def reserve_stock(req: ReservationRequest, x_tenant_id: str = Header()):
    """Reserve stock for an order, decrementing on-hand quantity.

    Reservations are idempotent per order_id: a repeated call for an order we
    already reserved is a no-op.
    """
    tenant_id = _tenant(x_tenant_id)

    existing = fetch_one(
        "SELECT 1 FROM reservations WHERE order_id = %s AND tenant_id = %s",
        (req.order_id, tenant_id),
    )
    if existing is not None:
        return {"order_id": req.order_id, "status": "already_reserved"}

    row = fetch_one(
        "SELECT quantity FROM items "
        "WHERE sku = %s AND warehouse_id = %s AND tenant_id = %s",
        (req.sku, req.warehouse_id, tenant_id),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if row["quantity"] < req.quantity:
        raise HTTPException(status_code=409, detail="insufficient stock")

    execute(
        "UPDATE items SET quantity = quantity - %s "
        "WHERE sku = %s AND warehouse_id = %s AND tenant_id = %s",
        (req.quantity, req.sku, req.warehouse_id, tenant_id),
    )
    execute(
        "INSERT INTO reservations (order_id, tenant_id, sku, warehouse_id, quantity) "
        "VALUES (%s, %s, %s, %s, %s)",
        (req.order_id, tenant_id, req.sku, req.warehouse_id, req.quantity),
    )
    cache.invalidate(cache.stock_key(req.sku))
    return {"order_id": req.order_id, "status": "reserved"}
