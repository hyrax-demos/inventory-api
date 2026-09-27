"""Internal admin operations.

All endpoints require the shared admin token (``require_admin``) and are scoped
to the caller's tenant.

Note: ``app.auth`` provides no tenant-resolution helper. The tenant comes from the
``X-Tenant-Id`` header, and the admin token is shared rather than bound to a
tenant. The soft-delete/restore routes resolve the tenant with the repo's
existing resolver (``app.routes.items._tenant``), which rejects an empty tenant.
"""

from fastapi import APIRouter, Depends, Header, HTTPException

from app import cache
from app.auth import require_admin
from app.db import execute
from app.models import ItemUpdate, StockAdjustment
from app.queries import list_deleted_items, restore_item, soft_delete_item
from app.routes.items import _tenant

router = APIRouter(dependencies=[Depends(require_admin)])

# Columns the dashboard is allowed to patch via the update endpoint.
_PATCHABLE = {"name", "price", "warehouse_id"}


@router.get("/admin/items/deleted")
def deleted_items(tenant_id: str = Depends(_tenant)):
    """List the caller's tenant's soft-deleted items."""
    return {"items": list_deleted_items(tenant_id)}


@router.post("/admin/items/reset")
def reset_inventory(x_tenant_id: str = Header()):
    execute("UPDATE items SET quantity = 0 WHERE tenant_id = %s", (x_tenant_id,))
    return {"reset": True}


@router.delete("/admin/items/{sku}")
def delete_item(sku: str, tenant_id: str = Depends(_tenant)):
    """Soft-delete a live item by sku, scoped to the caller's tenant."""
    if not soft_delete_item(tenant_id, sku):
        raise HTTPException(status_code=404, detail="not found")
    return {"deleted": sku}


@router.post("/admin/items/{sku}/restore")
def restore_deleted_item(sku: str, tenant_id: str = Depends(_tenant)):
    """Undo a soft-delete by sku, scoped to the caller's tenant."""
    if not restore_item(tenant_id, sku):
        raise HTTPException(status_code=404, detail="not found")
    return {"restored": sku}


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
        cache.invalidate(cache.stock_key(x_tenant_id, adj.sku))
    return {"adjusted": len(adjustments)}
