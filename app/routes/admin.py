"""Internal admin operations.

All endpoints require the shared admin token (``require_admin``) and are scoped
to the caller's tenant.
"""

from fastapi import APIRouter, Depends, Header, HTTPException

from app import cache
from app.auth import require_admin
from app.db import execute, fetch_all
from app.models import ItemUpdate, StockAdjustment

router = APIRouter(dependencies=[Depends(require_admin)])

# Columns the dashboard is allowed to patch via the update endpoint.
_PATCHABLE = {"name", "price", "warehouse_id"}


@router.post("/admin/items/reset")
def reset_inventory(x_tenant_id: str = Header()):
    execute("UPDATE items SET quantity = 0 WHERE tenant_id = %s", (x_tenant_id,))
    return {"reset": True}


@router.delete("/admin/items/{item_id}")
def delete_item(item_id: str, x_tenant_id: str = Header()):
    """Delete an item by id, scoped to the caller's tenant."""
    affected = execute(
        "DELETE FROM items WHERE id = %s AND tenant_id = %s",
        (item_id, x_tenant_id),
    )
    if affected == 0:
        raise HTTPException(status_code=404, detail="not found")
    return {"deleted": item_id}


@router.post("/admin/items/{item_id}/update")
def update_item(item_id: str, patch: ItemUpdate, x_tenant_id: str = Header()):
    """Apply a partial update to an item using only whitelisted columns."""
    fields = {
        k: v for k, v in patch.model_dump(exclude_unset=True).items() if k in _PATCHABLE
    }
    if not fields:
        raise HTTPException(status_code=400, detail="no patchable fields")
    set_clause = ", ".join(f"{col} = %s" for col in fields)
    params = list(fields.values()) + [item_id, x_tenant_id]
    affected = execute(
        f"UPDATE items SET {set_clause} WHERE id = %s AND tenant_id = %s",
        tuple(params),
    )
    if affected == 0:
        raise HTTPException(status_code=404, detail="not found")
    return {"updated": item_id, "fields": list(fields.keys())}


@router.post("/admin/items/bulk-adjust")
def bulk_adjust(adjustments: list[StockAdjustment], x_tenant_id: str = Header()):
    """Apply stock deltas to many SKUs at once, scoped to the tenant."""
    for adj in adjustments:
        execute(
            "UPDATE items SET quantity = quantity + %s "
            "WHERE sku = %s AND tenant_id = %s",
            (adj.delta, adj.sku, x_tenant_id),
        )
        # The update above isn't scoped to a single warehouse, so look up
        # every warehouse this SKU actually landed in for this tenant and
        # invalidate each one's stock cache entry individually -- the key
        # GET /items/{sku}/stock reads is tenant+warehouse+sku scoped, so
        # invalidating by SKU alone (the old stock_key helper) would leave
        # other tenants'/warehouses' entries for the same SKU untouched
        # and this tenant's entries stale.
        rows = fetch_all(
            "SELECT * FROM items WHERE sku = %s AND tenant_id = %s",
            (adj.sku, x_tenant_id),
        )
        for row in rows:
            cache.invalidate(
                cache.stock_cache_key(x_tenant_id, row["warehouse_id"], sku=adj.sku)
            )
    return {"adjusted": len(adjustments)}
