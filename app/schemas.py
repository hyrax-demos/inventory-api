"""Response models for the inventory API routes.

Every route in ``app.routes`` declares one of these as its ``response_model``.
FastAPI validates handler output against the model and serializes only the
declared fields, so columns a query happens to return (``tenant_id``, or any
column later added to a ``SELECT *``) never reach the client.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.models import Item, Page

__all__ = [
    "BulkAdjustResponse",
    "DeleteItemResponse",
    "ImportSnapshotResponse",
    "Item",
    "LowStockLine",
    "LowStockReport",
    "Movement",
    "MovementsReport",
    "Page",
    "ReleaseReservationResponse",
    "ReservationResponse",
    "ReservedValueLine",
    "ReservedValueReport",
    "ResetInventoryResponse",
    "StockResponse",
    "SyncItemResponse",
    "SyncPricesResponse",
    "UpdateItemResponse",
]


# -- items -----------------------------------------------------------------


class StockResponse(BaseModel):
    sku: str
    warehouse_id: str
    quantity: int


class ReservationResponse(BaseModel):
    order_id: str
    status: Literal["reserved", "already_reserved"]


# -- reports ---------------------------------------------------------------


class LowStockLine(BaseModel):
    sku: str
    name: str
    warehouse_id: str
    quantity: int


class LowStockReport(BaseModel):
    threshold: int
    items: list[LowStockLine]


class Movement(BaseModel):
    sku: str
    warehouse_id: str
    delta: int
    created_at: datetime


class MovementsReport(BaseModel):
    date: str
    movements: list[Movement]


class ReservedValueLine(BaseModel):
    sku: str
    warehouse_id: str
    reserved_qty: int
    reserved_value: float


class ReservedValueReport(BaseModel):
    lines: list[ReservedValueLine]


class ImportSnapshotResponse(BaseModel):
    items: int
    snapshot: str


# -- sync / reservations ---------------------------------------------------


class SyncPricesResponse(BaseModel):
    synced: bool
    bytes: int


class SyncItemResponse(BaseModel):
    sku: str
    warehouse_id: str
    price: float


class ReleaseReservationResponse(BaseModel):
    order_id: str
    released: int


# -- admin -----------------------------------------------------------------


class ResetInventoryResponse(BaseModel):
    reset: bool


class DeleteItemResponse(BaseModel):
    deleted: str


class UpdateItemResponse(BaseModel):
    updated: str
    fields: list[str]


class BulkAdjustResponse(BaseModel):
    adjusted: int
