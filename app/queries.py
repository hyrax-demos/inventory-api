"""Shared query-layer helpers for the ``items`` table.

Soft-delete: an item row with a non-NULL ``deleted_at`` has been soft-deleted
and must be invisible to every read path. Rows are never physically removed
by these helpers. Every SELECT that reads (or joins through) ``items`` should
include :func:`live_items` so this rule lives in exactly one place.
"""

from app import cache
from app.db import execute, fetch_all

# The one definition of "live" (not soft-deleted) item rows.
LIVE_ITEMS = "deleted_at IS NULL"


def live_items(alias: str = "") -> str:
    """Return the live-items SQL condition, optionally qualified by a table alias.

    ``live_items()`` -> ``deleted_at IS NULL``;
    ``live_items("i")`` -> ``i.deleted_at IS NULL``.
    """
    return f"{alias}.{LIVE_ITEMS}" if alias else LIVE_ITEMS


def soft_delete_item(tenant_id: str, sku: str) -> bool:
    """Mark a live item deleted for one tenant. Return whether a row changed.

    Only rows matching both tenant and sku that are not already deleted are
    touched, so repeating the call is a no-op that returns False.
    """
    affected = execute(
        "UPDATE items SET deleted_at = now() "
        "WHERE tenant_id = %s AND sku = %s AND deleted_at IS NULL",
        (tenant_id, sku),
    )
    if affected:
        # The stock cache is read before the DB; drop it so the deleted item
        # stops being served from cache immediately.
        cache.invalidate(cache.stock_key(tenant_id, sku))
    return affected > 0


def restore_item(tenant_id: str, sku: str) -> bool:
    """Clear the soft-delete mark for one tenant's item. Return whether a row changed.

    Only rows matching both tenant and sku that are currently deleted are
    touched; restoring a live or missing item returns False.
    """
    affected = execute(
        "UPDATE items SET deleted_at = NULL "
        "WHERE tenant_id = %s AND sku = %s AND deleted_at IS NOT NULL",
        (tenant_id, sku),
    )
    if affected:
        cache.invalidate(cache.stock_key(tenant_id, sku))
    return affected > 0


def list_deleted_items(tenant_id: str) -> list[dict]:
    """Return one tenant's soft-deleted items, most recently deleted first."""
    return fetch_all(
        "SELECT * FROM items WHERE tenant_id = %s AND deleted_at IS NOT NULL "
        "ORDER BY deleted_at DESC, id ASC",
        (tenant_id,),
    )
