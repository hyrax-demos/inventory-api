"""Read-only reporting queries."""

from datetime import datetime

from app.db import fetch_all


def low_stock(tenant_id: str, threshold: int) -> list[dict]:
    """Items at or below ``threshold``, lowest quantity first."""
    return fetch_all(
        "SELECT sku, name, warehouse_id, quantity FROM items "
        "WHERE tenant_id = %s AND quantity <= %s ORDER BY quantity ASC",
        (tenant_id, threshold),
    )


def movements_since(tenant_id: str, since: datetime) -> list[dict]:
    """Stock movements created at or after ``since``, oldest first."""
    return fetch_all(
        "SELECT sku, warehouse_id, delta, created_at FROM movements "
        "WHERE tenant_id = %s AND created_at >= %s ORDER BY created_at ASC",
        (tenant_id, since),
    )


def reserved_value_by_sku(tenant_id: str) -> list[dict]:
    """Reserved quantity and priced value per (sku, warehouse), highest first."""
    return fetch_all(
        "SELECT r.sku, r.warehouse_id, "
        "       SUM(r.quantity) AS reserved_qty, "
        "       SUM(r.quantity * i.price) AS reserved_value "
        "FROM reservations r "
        "JOIN items i "
        "  ON i.sku = r.sku AND i.warehouse_id = r.warehouse_id "
        "WHERE r.tenant_id = %s "
        "GROUP BY r.sku, r.warehouse_id "
        "ORDER BY reserved_value DESC",
        (tenant_id,),
    )
