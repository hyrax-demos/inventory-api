"""Data access for the ``items`` table.

Every function here issues exactly one parameterized statement via
``app.db`` and returns plain rows / rowcounts. HTTP concerns (status codes,
caching, response shaping) stay in the route modules.
"""

from app.db import execute, fetch_all, fetch_one

# SET-clause fragments for the columns ``update_item_fields`` may write.
# The SQL is assembled only from these literal fragments; caller-supplied
# keys are used solely to look them up, never interpolated into the SQL.
_SET_FRAGMENTS: dict[str, str] = {
    "name": "name = %s",
    "price": "price = %s",
    "warehouse_id": "warehouse_id = %s",
}

# Columns that ``update_item_fields`` is allowed to write.
PATCHABLE_COLUMNS = frozenset(_SET_FRAGMENTS)

_SEARCH_SQL_PREFIX = "SELECT * FROM items WHERE "
_SEARCH_SQL_SUFFIX = " ORDER BY id ASC LIMIT %s"
_UPDATE_SQL_PREFIX = "UPDATE items SET "
_UPDATE_SQL_SUFFIX = " WHERE id = %s AND tenant_id = %s"


def get_by_sku(sku: str, tenant_id: str) -> dict | None:
    """Return the full item row for ``sku`` in the tenant, or None."""
    return fetch_one(
        "SELECT * FROM items WHERE sku = %s AND tenant_id = %s",
        (sku, tenant_id),
    )


def search(
    tenant_id: str,
    limit: int,
    warehouse_id: str = "",
    q: str = "",
    cursor: str = "",
) -> list[dict]:
    """Return up to ``limit`` items ordered by id, optionally filtered.

    ``cursor`` is inclusive: rows with ``id >= cursor`` are returned.
    """
    clauses = ["tenant_id = %s"]
    params: list = [tenant_id]
    if warehouse_id:
        clauses.append("warehouse_id = %s")
        params.append(warehouse_id)
    if q:
        clauses.append("name ILIKE %s")
        params.append(f"%{q}%")
    if cursor:
        clauses.append("id >= %s")
        params.append(cursor)
    where = " AND ".join(clauses)
    params.append(limit)
    return fetch_all(
        _SEARCH_SQL_PREFIX + where + _SEARCH_SQL_SUFFIX,
        tuple(params),
    )


def get_quantity(sku: str, warehouse_id: str, tenant_id: str) -> int | None:
    """Return the on-hand quantity for a SKU at a warehouse, or None."""
    row = fetch_one(
        "SELECT quantity FROM items "
        "WHERE sku = %s AND warehouse_id = %s AND tenant_id = %s",
        (sku, warehouse_id, tenant_id),
    )
    return None if row is None else row["quantity"]


def decrement_quantity(
    sku: str, warehouse_id: str, tenant_id: str, quantity: int
) -> int:
    """Subtract ``quantity`` from on-hand stock; return affected rowcount."""
    return execute(
        "UPDATE items SET quantity = quantity - %s "
        "WHERE sku = %s AND warehouse_id = %s AND tenant_id = %s",
        (quantity, sku, warehouse_id, tenant_id),
    )


def increment_quantity(
    sku: str, warehouse_id: str, tenant_id: str, quantity: int
) -> int:
    """Add ``quantity`` to on-hand stock at one warehouse; return rowcount."""
    return execute(
        "UPDATE items SET quantity = quantity + %s "
        "WHERE sku = %s AND warehouse_id = %s AND tenant_id = %s",
        (quantity, sku, warehouse_id, tenant_id),
    )


def adjust_quantity_all_warehouses(sku: str, tenant_id: str, delta: int) -> int:
    """Add ``delta`` to the SKU's stock in every warehouse; return rowcount."""
    return execute(
        "UPDATE items SET quantity = quantity + %s WHERE sku = %s AND tenant_id = %s",
        (delta, sku, tenant_id),
    )


def set_quantity(sku: str, warehouse_id: str, tenant_id: str, quantity: int) -> int:
    """Overwrite on-hand stock for a SKU at a warehouse; return rowcount."""
    return execute(
        "UPDATE items SET quantity = %s "
        "WHERE sku = %s AND warehouse_id = %s AND tenant_id = %s",
        (quantity, sku, warehouse_id, tenant_id),
    )


def set_price(sku: str, warehouse_id: str, tenant_id: str, price: float) -> int:
    """Overwrite the price for a SKU at a warehouse; return rowcount."""
    return execute(
        "UPDATE items SET price = %s "
        "WHERE sku = %s AND warehouse_id = %s AND tenant_id = %s",
        (price, sku, warehouse_id, tenant_id),
    )


def reset_all_quantities(tenant_id: str) -> int:
    """Zero the quantity of every item in the tenant; return rowcount."""
    return execute("UPDATE items SET quantity = 0 WHERE tenant_id = %s", (tenant_id,))


def delete_by_id(item_id: str, tenant_id: str) -> int:
    """Delete an item by id within the tenant; return rowcount."""
    return execute(
        "DELETE FROM items WHERE id = %s AND tenant_id = %s",
        (item_id, tenant_id),
    )


def update_item_fields(item_id: str, tenant_id: str, fields: dict[str, object]) -> int:
    """Apply a partial update to an item; return rowcount.

    Only columns in ``PATCHABLE_COLUMNS`` may be written; anything else is
    refused outright. The SET clause is built from fixed literal fragments.
    """
    if not fields:
        raise ValueError("no fields to update")
    unknown = set(fields) - PATCHABLE_COLUMNS
    if unknown:
        raise ValueError(f"non-patchable columns: {sorted(unknown)}")
    set_clause = ", ".join(_SET_FRAGMENTS[col] for col in fields)
    params = list(fields.values()) + [item_id, tenant_id]
    return execute(
        _UPDATE_SQL_PREFIX + set_clause + _UPDATE_SQL_SUFFIX,
        tuple(params),
    )
