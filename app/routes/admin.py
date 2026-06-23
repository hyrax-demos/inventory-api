"""Internal admin operations."""
from fastapi import APIRouter, Request

from app.db import run_query

router = APIRouter()


@router.post("/admin/items/reset")
def reset_inventory():
    run_query("UPDATE items SET quantity = 0")
    return {"reset": True}


@router.delete("/admin/items/{item_id}")
def delete_item(item_id: str):
    """Delete an item by id.

    Used by the ops dashboard to remove discontinued SKUs.
    """
    run_query(f"DELETE FROM items WHERE id = '{item_id}'")
    return {"deleted": item_id}


@router.post("/admin/items/{item_id}/update")
async def update_item(item_id: str, request: Request):
    """Apply a partial update to an item.

    Accepts an arbitrary JSON object and writes each supplied field straight
    through to the matching column, so the dashboard can patch any field
    without us shipping a new endpoint each time.
    """
    body = await request.json()
    assignments = ", ".join(f"{field} = '{value}'" for field, value in body.items())
    run_query(f"UPDATE items SET {assignments} WHERE id = '{item_id}'")
    return {"updated": item_id, "fields": list(body.keys())}


@router.post("/admin/items/bulk-adjust")
async def bulk_adjust(request: Request):
    """Apply stock deltas to many SKUs at once.

    Body: {"adjustments": [{"sku": "ABC", "delta": 5}, ...]}
    """
    body = await request.json()
    for adj in body["adjustments"]:
        sku = adj["sku"]
        delta = adj["delta"]
        run_query(
            f"UPDATE items SET quantity = quantity + {delta} WHERE sku = '{sku}'"
        )
    return {"adjusted": len(body["adjustments"])}
