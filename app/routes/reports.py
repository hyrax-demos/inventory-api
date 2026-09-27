"""Report generation and snapshot import."""

import csv
import io
import json
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Response

from app.db import execute, fetch_all

router = APIRouter()

LOW_STOCK_CSV_COLUMNS = ("sku", "name", "warehouse_id", "quantity")


def low_stock_items(
    tenant_id: str, threshold: int, warehouse_id: str = ""
) -> list[dict]:
    """Items at or below ``threshold`` for ``tenant_id``, lowest quantity first.

    When ``warehouse_id`` is non-empty, only that warehouse's items are returned.
    """
    clauses = ["tenant_id = %s", "quantity <= %s"]
    params: list = [tenant_id, threshold]
    if warehouse_id:
        clauses.append("warehouse_id = %s")
        params.append(warehouse_id)
    where = " AND ".join(clauses)
    return fetch_all(
        "SELECT sku, name, warehouse_id, quantity FROM items "
        f"WHERE {where} ORDER BY quantity ASC",
        tuple(params),
    )


def low_stock_csv(rows: list[dict]) -> str:
    """Serialize low-stock rows as RFC 4180 CSV with a header row."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(LOW_STOCK_CSV_COLUMNS)
    for row in rows:
        writer.writerow([row[col] for col in LOW_STOCK_CSV_COLUMNS])
    return buf.getvalue()


@router.get("/reports/low-stock")
def low_stock_report(threshold: int = 10, x_tenant_id: str = Header()):
    """Items at or below the reorder threshold, scoped to the tenant."""
    rows = low_stock_items(x_tenant_id, threshold)
    return {"threshold": threshold, "items": rows}


@router.get("/reports/low-stock.csv")
def low_stock_report_csv(
    threshold: int = 10, warehouse_id: str = "", x_tenant_id: str = Header()
):
    """CSV export of the low-stock report (same filtering as the JSON endpoint).

    Optionally narrowed to a single warehouse with ``?warehouse_id=``.
    """
    rows = low_stock_items(x_tenant_id, threshold, warehouse_id)
    return Response(content=low_stock_csv(rows), media_type="text/csv; charset=utf-8")


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
