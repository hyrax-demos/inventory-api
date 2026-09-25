"""Report generation and snapshot import."""

import json
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException

from app.db import fetch_all, transaction
from app.routes.items import _tenant

router = APIRouter()


@router.get("/reports/low-stock")
def low_stock_report(threshold: int = 10, x_tenant_id: str = Header()):
    """Items at or below the reorder threshold, scoped to the tenant."""
    tenant_id = _tenant(x_tenant_id)
    rows = fetch_all(
        "SELECT sku, name, warehouse_id, quantity FROM items "
        "WHERE tenant_id = %s AND quantity <= %s ORDER BY quantity ASC",
        (tenant_id, threshold),
    )
    return {"threshold": threshold, "items": rows}


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
        "  AND i.tenant_id = r.tenant_id "
        "WHERE r.tenant_id = %s "
        "GROUP BY r.sku, r.warehouse_id "
        "ORDER BY reserved_value DESC",
        (x_tenant_id,),
    )
    return {"lines": rows}


def _parse_snapshot_entry(entry) -> tuple[str, str, int]:
    """Validate one snapshot entry; raise 400 if it is malformed."""
    malformed = HTTPException(status_code=400, detail="malformed snapshot entry")
    if not isinstance(entry, dict):
        raise malformed
    try:
        sku = entry["sku"]
        warehouse_id = entry["warehouse_id"]
        raw_quantity = entry["quantity"]
    except KeyError:
        raise malformed
    if not isinstance(sku, str) or not sku:
        raise malformed
    if not isinstance(warehouse_id, str) or not warehouse_id:
        raise malformed
    if isinstance(raw_quantity, bool):
        raise malformed
    try:
        quantity = int(raw_quantity)
    except (TypeError, ValueError):
        raise malformed
    if quantity < 0:
        raise malformed
    return sku, warehouse_id, quantity


@router.post("/reports/import")
async def import_snapshot(payload: dict, x_tenant_id: str = Header()):
    """Bulk-import a stock snapshot.

    Body: {"items": [{"sku": "ABC", "warehouse_id": "w1", "quantity": 5}, ...]}
    """
    items = payload.get("items")
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="items must be a list")
    # Validate every entry before touching the DB so a malformed entry
    # anywhere in the list means nothing is written.
    updates = [_parse_snapshot_entry(entry) for entry in items]
    # Apply every update on one connection in one transaction: a failure
    # on any statement rolls back all of them.
    with transaction() as conn:
        cur = conn.cursor()
        for sku, warehouse_id, quantity in updates:
            cur.execute(
                "UPDATE items SET quantity = %s "
                "WHERE sku = %s AND warehouse_id = %s AND tenant_id = %s",
                (quantity, sku, warehouse_id, x_tenant_id),
            )
    count = len(updates)
    return {"items": count, "snapshot": json.dumps({"received": count})}
