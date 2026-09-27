"""Data access for the ``reservations`` table."""

from app.db import execute, fetch_one


def exists(order_id: str, tenant_id: str) -> bool:
    """Whether a reservation already exists for the order in the tenant."""
    row = fetch_one(
        "SELECT 1 FROM reservations WHERE order_id = %s AND tenant_id = %s",
        (order_id, tenant_id),
    )
    return row is not None


def get(order_id: str, tenant_id: str) -> dict | None:
    """Return ``{sku, warehouse_id, quantity}`` for the order, or None."""
    return fetch_one(
        "SELECT sku, warehouse_id, quantity FROM reservations "
        "WHERE order_id = %s AND tenant_id = %s",
        (order_id, tenant_id),
    )


def create(
    order_id: str, tenant_id: str, sku: str, warehouse_id: str, quantity: int
) -> int:
    """Insert a reservation row; return affected rowcount."""
    return execute(
        "INSERT INTO reservations (order_id, tenant_id, sku, warehouse_id, quantity) "
        "VALUES (%s, %s, %s, %s, %s)",
        (order_id, tenant_id, sku, warehouse_id, quantity),
    )


def delete(order_id: str, tenant_id: str) -> int:
    """Delete the reservation for the order; return affected rowcount."""
    return execute(
        "DELETE FROM reservations WHERE order_id = %s AND tenant_id = %s",
        (order_id, tenant_id),
    )
