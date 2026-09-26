"""Report generation and snapshot import."""

import json
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException

from app.repositories import items as items_repo
from app.repositories import reports as reports_repo

router = APIRouter()


@router.get("/reports/low-stock")
def low_stock_report(threshold: int = 10, x_tenant_id: str = Header()):
    """Items at or below the reorder threshold, scoped to the tenant."""
    rows = reports_repo.low_stock(x_tenant_id, threshold)
    return {"threshold": threshold, "items": rows}


@router.get("/reports/today")
def todays_movements(x_tenant_id: str = Header()):
    """Stock movements recorded so far today.

    ``movements.created_at`` is stored in UTC; we report everything from the
    start of the current day onward.
    """
    start_of_day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = reports_repo.movements_since(x_tenant_id, start_of_day)
    return {"date": start_of_day.date().isoformat(), "movements": rows}


@router.get("/reports/reserved-value")
def reserved_value(x_tenant_id: str = Header()):
    """Total dollar value of stock currently reserved, by SKU.

    Joins open reservations to their item rows to price each reservation.
    """
    rows = reports_repo.reserved_value_by_sku(x_tenant_id)
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
        items_repo.set_quantity(sku, warehouse_id, x_tenant_id, quantity)
        count += 1
    return {"items": count, "snapshot": json.dumps({"received": count})}
