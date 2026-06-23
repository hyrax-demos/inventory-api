"""Domain models for the inventory API."""
from pydantic import BaseModel


class Item(BaseModel):
    id: str
    sku: str
    name: str
    quantity: int
    warehouse: str
    price: float = 0.0


class StockAdjustment(BaseModel):
    sku: str
    delta: int
