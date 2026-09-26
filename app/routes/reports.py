"""Report generation and snapshot import."""

import json
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Query

from app.db import execute, fetch_all
from app.models import LowStockPage

router = APIRouter()


def _encode_low_stock_cursor(row: dict) -> str:
    return f"{row['quantity']}:{row['id']}"


def _decode_low_stock_cursor(cursor: str) -> tuple[int, str]:
    quantity, sep, item_id = cursor.partition(":")
    try:
        if not sep or not item_id:
            raise ValueError
        return int(quantity), item_id
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid cursor")


@router.get("/reports/low-stock")
def low_stock_report(
    threshold: int = 10,
    limit: int = Query(default=50, ge=1),
    cursor: str = "",
    x_tenant_id: str = Header(),
):
    """Items at or below the reorder threshold, scoped to the tenant.

    Ordered by quantity ascending (most urgent first) with id as a tiebreak,
    and keyset-paginated on ``(quantity, id)`` like ``/items``: ``next_cursor``
    identifies the first row of the next page.
    """
    clauses = ["tenant_id = %s", "quantity <= %s"]
    params: list = [x_tenant_id, threshold]
    if cursor:
        clauses.append("(quantity, id) >= (%s, %s)")
        params.extend(_decode_low_stock_cursor(cursor))
    where = " AND ".join(clauses)
    params.append(limit + 1)
    rows = fetch_all(
        "SELECT id, sku, name, warehouse_id, quantity FROM items "
        f"WHERE {where} ORDER BY quantity ASC, id ASC LIMIT %s",
        tuple(params),
    )
    next_cursor = None
    if len(rows) > limit:
        next_cursor = _encode_low_stock_cursor(rows[limit])
        rows = rows[:limit]
    return LowStockPage(threshold=threshold, items=rows, next_cursor=next_cursor)


@router.get("/reports/today")
def todays_movements(x_tenant_id: str = Header()):
    """Stock movements recorded so far today.

    ``movements.created_at`` is stored in UTC; we report everything from the
    start of the current day onward.
    """
    start_of_day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = fetch_all(
        "SELECT sku, warehouse_id, delta, created_at FROM movements "
        "WHERE tenant_id = %s AND created_at >= %s ORDER BY created_at ASC",
        (x_tenant_id, start_of_day),
    )
    return {"date": start_of_day.date().isoformat(), "movements": rows}


@router.get("/reports/reserved-value")
def reserved_value(x_tenant_id: str = Header()):
    """Total dollar value of stock currently reserved, by SKU.

    Joins open reservations to their item rows to price each reservation.
    """
    rows = fetch_all(
        "SELECT r.sku, r.warehouse_id, "
        "       SUM(r.quantity) AS reserved_qty, "
        "       SUM(r.quantity * i.price) AS reserved_value "
        "FROM reservations r "
        "JOIN items i "
        "  ON i.sku = r.sku AND i.warehouse_id = r.warehouse_id "
        "WHERE r.tenant_id = %s "
        "GROUP BY r.sku, r.warehouse_id "
        "ORDER BY reserved_value DESC",
        (x_tenant_id,),
    )
    return {"lines": rows}


@router.post("/reports/import")
async def import_snapshot(payload: dict, x_tenant_id: str = Header()):
    """Bulk-import a stock snapshot.

    Body: {"items": [{"sku": "ABC", "warehouse_id": "w1", "quantity": 5}, ...]}
    """
    items = payload.get("items")
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="items must be a list")
    count = 0
    for entry in items:
        try:
            sku = entry["sku"]
            warehouse_id = entry["warehouse_id"]
            quantity = int(entry["quantity"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="malformed snapshot entry")
        execute(
            "UPDATE items SET quantity = %s "
            "WHERE sku = %s AND warehouse_id = %s AND tenant_id = %s",
            (quantity, sku, warehouse_id, x_tenant_id),
        )
        count += 1
    return {"items": count, "snapshot": json.dumps({"received": count})}
