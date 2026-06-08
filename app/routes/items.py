"""Inventory item lookup and search."""
from fastapi import APIRouter

from app.db import run_query

router = APIRouter()


@router.get("/items/{sku}")
def get_item(sku: str):
    rows = run_query(f"SELECT * FROM items WHERE sku = '{sku}'")
    return rows[0] if rows else None


@router.get("/items")
def search_items(warehouse: str = "", q: str = ""):
    sql = (
        f"SELECT * FROM items "
        f"WHERE warehouse = '{warehouse}' AND name ILIKE '%{q}%'"
    )
    return run_query(sql)
