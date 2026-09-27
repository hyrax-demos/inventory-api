"""Internal admin operations.

All endpoints require the shared admin token (``require_admin``) and are scoped
to the caller's tenant.
"""

from fastapi import APIRouter, Depends, Header, HTTPException

from app import cache
from app.auth import require_admin
from app.models import ItemUpdate, StockAdjustment
from app.repositories import items as items_repo
from app.schemas import (
    BulkAdjustResponse,
    DeleteItemResponse,
    ResetInventoryResponse,
    UpdateItemResponse,
)

router = APIRouter(dependencies=[Depends(require_admin)])

# Columns the dashboard is allowed to patch via the update endpoint.
_PATCHABLE = items_repo.PATCHABLE_COLUMNS


@router.post("/admin/items/reset", response_model=ResetInventoryResponse)
def reset_inventory(x_tenant_id: str = Header()):
    items_repo.reset_all_quantities(x_tenant_id)
    return {"reset": True}


@router.delete("/admin/items/{item_id}", response_model=DeleteItemResponse)
def delete_item(item_id: str, x_tenant_id: str = Header()):
    """Delete an item by id, scoped to the caller's tenant."""
    affected = items_repo.delete_by_id(item_id, x_tenant_id)
    if affected == 0:
        raise HTTPException(status_code=404, detail="not found")
    return {"deleted": item_id}


@router.post("/admin/items/{item_id}/update", response_model=UpdateItemResponse)
def update_item(item_id: str, patch: ItemUpdate, x_tenant_id: str = Header()):
    """Apply a partial update to an item using only whitelisted columns."""
    fields = {
        k: v for k, v in patch.model_dump(exclude_unset=True).items() if k in _PATCHABLE
    }
    if not fields:
        raise HTTPException(status_code=400, detail="no patchable fields")
    affected = items_repo.update_item_fields(item_id, x_tenant_id, fields)
    if affected == 0:
        raise HTTPException(status_code=404, detail="not found")
    return {"updated": item_id, "fields": list(fields.keys())}


@router.post("/admin/items/bulk-adjust", response_model=BulkAdjustResponse)
def bulk_adjust(adjustments: list[StockAdjustment], x_tenant_id: str = Header()):
    """Apply stock deltas to many SKUs at once, scoped to the tenant."""
    for adj in adjustments:
        items_repo.adjust_quantity_all_warehouses(adj.sku, x_tenant_id, adj.delta)
        cache.invalidate(cache.stock_key(adj.sku))
    return {"adjusted": len(adjustments)}
