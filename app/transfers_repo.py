"""Data access for stock transfers.

Every function takes an open DB connection (``conn``) and only runs
statements on it. None of them commits, rolls back or closes it, so the
caller can run several inside one ``app.db.transaction()`` and get
all-or-nothing behaviour::

    with transaction() as conn:
        if not decrement_stock(conn, tenant_id, src, sku, qty):
            raise InsufficientStock
        increment_stock(conn, tenant_id, dst, sku, qty)
        transfer = insert_transfer(conn, tenant_id, sku, src, dst, qty)
        insert_movement(conn, tenant_id, sku, src, -qty, "transfer_out", transfer["id"])
        insert_movement(conn, tenant_id, sku, dst, qty, "transfer_in", transfer["id"])

Every statement is filtered by ``tenant_id``. All SQL is parameterized.
Schema: migrations/0001_create_transfers.up.sql.
"""

from datetime import datetime

import psycopg2.extras

MOVEMENT_TRANSFER_IN = "transfer_in"
MOVEMENT_TRANSFER_OUT = "transfer_out"

# All SQL below is static string literals with %s placeholders; nothing is
# built by string formatting.
_INSERT_TRANSFER_SQL = (
    "INSERT INTO transfers "
    "(tenant_id, sku, source_warehouse_id, destination_warehouse_id, quantity) "
    "VALUES (%s, %s, %s, %s, %s) "
    "RETURNING id, tenant_id, sku, source_warehouse_id, "
    "destination_warehouse_id, quantity, created_at"
)

_LIST_TRANSFERS_SQL = (
    "SELECT id, tenant_id, sku, source_warehouse_id, "
    "destination_warehouse_id, quantity, created_at "
    "FROM transfers "
    "WHERE tenant_id = %s "
    "ORDER BY created_at DESC, id DESC LIMIT %s"
)

_LIST_TRANSFERS_AFTER_SQL = (
    "SELECT id, tenant_id, sku, source_warehouse_id, "
    "destination_warehouse_id, quantity, created_at "
    "FROM transfers "
    "WHERE tenant_id = %s AND (created_at, id) < (%s, %s) "
    "ORDER BY created_at DESC, id DESC LIMIT %s"
)


def _dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def insert_transfer(
    conn,
    tenant_id: str,
    sku: str,
    source_warehouse_id: str,
    destination_warehouse_id: str,
    quantity: int,
) -> dict:
    """Insert a transfer row and return it (with its generated id/created_at)."""
    cur = _dict_cursor(conn)
    cur.execute(
        _INSERT_TRANSFER_SQL,
        (tenant_id, sku, source_warehouse_id, destination_warehouse_id, quantity),
    )
    return dict(cur.fetchone())


def list_transfers(
    conn,
    tenant_id: str,
    limit: int,
    after: tuple[datetime, str] | None = None,
) -> list[dict]:
    """Return up to ``limit`` of the tenant's transfers, newest first.

    Ordered by (created_at DESC, id DESC). With ``after=(created_at, id)``,
    return only rows strictly after that cursor in that order (keyset
    pagination, no OFFSET). To see whether there is another page, ask for
    ``limit + 1`` rows.
    """
    cur = _dict_cursor(conn)
    if after is None:
        cur.execute(_LIST_TRANSFERS_SQL, (tenant_id, limit))
    else:
        cursor_created_at, cursor_id = after
        cur.execute(
            _LIST_TRANSFERS_AFTER_SQL,
            (tenant_id, cursor_created_at, cursor_id, limit),
        )
    return [dict(r) for r in cur.fetchall()]


def decrement_stock(
    conn, tenant_id: str, warehouse_id: str, sku: str, quantity: int
) -> bool:
    """Subtract ``quantity`` from a stock row, but only if it has enough.

    It's a single conditional UPDATE, so two transfers racing each other
    can't push the stock negative. Returns True if a row was decremented.
    Returns False if there wasn't enough stock or there was no row at all.
    """
    cur = conn.cursor()
    cur.execute(
        "UPDATE items SET quantity = quantity - %s "
        "WHERE tenant_id = %s AND warehouse_id = %s AND sku = %s "
        "AND quantity >= %s",
        (quantity, tenant_id, warehouse_id, sku, quantity),
    )
    return cur.rowcount == 1


def increment_stock(
    conn, tenant_id: str, warehouse_id: str, sku: str, quantity: int
) -> None:
    """Add ``quantity`` to a stock row, creating the row if it's missing.

    A new row takes the SKU's name/price from the tenant's existing row for
    that SKU (in a transfer that is the source row). It relies on the
    unique (tenant_id, warehouse_id, sku) index from migration 0001.
    """
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO items (tenant_id, warehouse_id, sku, name, price, quantity) "
        "SELECT %s, %s, %s, "
        "  COALESCE((SELECT name FROM items WHERE tenant_id = %s AND sku = %s LIMIT 1), %s), "
        "  COALESCE((SELECT price FROM items WHERE tenant_id = %s AND sku = %s LIMIT 1), 0), "
        "  %s "
        "ON CONFLICT (tenant_id, warehouse_id, sku) "
        "DO UPDATE SET quantity = items.quantity + EXCLUDED.quantity",
        (
            tenant_id,
            warehouse_id,
            sku,
            tenant_id,
            sku,
            sku,
            tenant_id,
            sku,
            quantity,
        ),
    )


def insert_movement(
    conn,
    tenant_id: str,
    sku: str,
    warehouse_id: str,
    delta: int,
    reason: str | None = None,
    transfer_id: str | None = None,
) -> None:
    """Record a stock movement (negative delta = outbound, positive = inbound).

    Nothing else in the repo writes to ``movements`` yet, so there is no
    existing helper to reuse. This one writes the columns that
    ``/reports/today`` reads, plus the reason/transfer_id columns from
    migration 0001.
    """
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO movements "
        "(tenant_id, sku, warehouse_id, delta, reason, transfer_id, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, now())",
        (tenant_id, sku, warehouse_id, delta, reason, transfer_id),
    )
