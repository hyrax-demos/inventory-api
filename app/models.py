"""Domain models for the inventory API."""

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

# A string that must contain at least one non-whitespace character.
NonEmptyStr = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]


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
    sku: NonEmptyStr
    warehouse_id: NonEmptyStr
    quantity: int = Field(ge=1)
    order_id: NonEmptyStr


class ItemUpdate(BaseModel):
    """Whitelisted fields the ops dashboard may patch on an item."""

    name: str | None = None
    price: float | None = None
    warehouse_id: str | None = None


class Page(BaseModel):
    """A page of results plus an opaque cursor for the next page."""

    items: list[dict]
    next_cursor: str | None = None
