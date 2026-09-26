"""Stock transfers between a tenant's warehouses.

Handlers stay thin: they resolve the tenant from the request headers, shape
the input and call ``app.transfers_service``. Domain errors raised by the
service are turned into HTTP responses by the handler registered in
``app.main``.
"""

import json

from fastapi import APIRouter, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app import transfers_service
from app.errors import ValidationError
from app.models import TransferOut

router = APIRouter()

_ID_FIELDS = ("sku", "source_warehouse_id", "destination_warehouse_id")


def _tenant(x_tenant_id: str) -> str:
    # Same rule as the other tenant-scoped routes. The tenant only ever comes
    # from the request header, never from the body or the query string.
    if not x_tenant_id:
        raise HTTPException(status_code=400, detail="missing tenant")
    return x_tenant_id


async def _read_json_object(request: Request) -> dict:
    try:
        payload = json.loads(await request.body() or b"null")
    except (ValueError, UnicodeDecodeError):
        raise ValidationError(
            "invalid_body", "request body must be valid JSON"
        ) from None
    if not isinstance(payload, dict):
        raise ValidationError("invalid_body", "request body must be a JSON object")
    return payload


def _parse_create(payload: dict) -> dict:
    """Schema-check the POST body. Only the four known fields are read."""
    fields = {}
    for name in _ID_FIELDS:
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("invalid_field", f"{name} is required")
        fields[name] = value
    quantity = payload.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ValidationError("invalid_quantity", "quantity must be an integer")
    fields["quantity"] = quantity
    return fields


def _serialize(item) -> dict:
    return TransferOut.model_validate(
        item.model_dump() if hasattr(item, "model_dump") else item
    ).model_dump(mode="json")


@router.post("/transfers", status_code=201)
async def create_transfer(request: Request, x_tenant_id: str = Header()):
    """Move stock of one SKU from a source to a destination warehouse."""
    tenant_id = _tenant(x_tenant_id)
    fields = _parse_create(await _read_json_object(request))
    # The service does blocking DB work; run it off the event loop the same
    # way FastAPI runs sync handlers.
    transfer = await run_in_threadpool(
        transfers_service.create_transfer, tenant_id, **fields
    )
    return _serialize(transfer)


@router.get("/transfers")
def list_transfers(
    limit: str | None = None,
    cursor: str | None = None,
    x_tenant_id: str = Header(),
):
    """The caller's tenant's transfers, newest first, keyset-paginated."""
    tenant_id = _tenant(x_tenant_id)
    # ``limit`` is passed through as text so the service owns its validation
    # (integer, 1..100) and returns the repo's 400 rather than FastAPI's 422.
    page = transfers_service.list_transfers(tenant_id, limit=limit, cursor=cursor)
    return {
        "items": [_serialize(item) for item in page.items],
        "next_cursor": page.next_cursor,
    }
