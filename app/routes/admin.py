"""Internal admin operations."""
from fastapi import APIRouter

from app.db import run_query

router = APIRouter()


@router.post("/admin/items/reset")
def reset_inventory():
    run_query("UPDATE items SET quantity = 0")
    return {"reset": True}
