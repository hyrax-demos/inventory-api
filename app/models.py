"""Domain models for the inventory API."""
from pydantic import BaseModel


class Item(BaseModel):
    id: str
    sku: str
    name: str
    quantity: int
    warehouse_id: str
    price: float = 0.0


class StockAdjustment(BaseModel):
    sku: str
    delta: int


class ReservationRequest(BaseModel):
    sku: str
    warehouse_id: str
    quantity: int
    order_id: str


class ItemUpdate(BaseModel):
    """Whitelisted fields the ops dashboard may patch on an item."""

    name: str | None = None
    price: float | None = None
    warehouse_id: str | None = None


class Page(BaseModel):
    """A page of results plus an opaque cursor for the next page."""

    items: list[dict]
    next_cursor: str | None = None
