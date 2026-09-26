"""Stock transfer business logic.

HTTP handlers should stay thin and call these functions. They raise
``app.errors`` domain errors, never ``HTTPException``.

``create_transfer`` makes every write (source decrement, destination
upsert, transfer row, both movements rows) on one connection inside one
``app.db.transaction()``, so either all of them commit or none do. The
stock cache is invalidated only after that block exits normally, which
means after the commit. A failed or rolled-back transfer never touches
the cache.
"""

import base64
import binascii
import uuid
from datetime import datetime

from app import cache, db, transfers_repo
from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import Page, Transfer

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def _require_id(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("invalid_field", f"{field} is required")
    return value


def _validate_quantity(quantity) -> int:
    # bool is a subclass of int; True must not count as quantity 1.
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ValidationError("invalid_quantity", "quantity must be an integer")
    if quantity <= 0:
        raise ValidationError("invalid_quantity", "quantity must be greater than 0")
    return quantity


def _stock_cache_keys(sku: str) -> list[str]:
    """Stock cache keys a transfer of ``sku`` makes stale.

    Today the cache module keeps a single stock key per SKU
    (``cache.stock_key(sku)``). It covers every warehouse of that SKU, so
    this key covers both the source side and the destination side. Price
    keys do not change on a transfer.
    """
    return [cache.stock_key(sku)]


def create_transfer(
    tenant_id: str,
    sku: str,
    source_warehouse_id: str,
    destination_warehouse_id: str,
    quantity: int,
    actor: str | None = None,
) -> Transfer:
    """Move ``quantity`` of ``sku`` between two of the tenant's warehouses.

    Checks run in this order:

    1. ``ValidationError``: a field is missing, or quantity is not a
       positive integer (``invalid_quantity``).
    2. ``ValidationError``: the source and destination are the same
       warehouse (``same_warehouse``).
    3. ``NotFoundError``: the source warehouse, destination warehouse or
       SKU does not exist within the tenant.
    4. ``ConflictError``: the source does not have enough stock
       (``insufficient_stock``).

    ``actor`` is accepted for callers that know who made the request. The
    transfers table has no created_by column, so it is not stored yet.
    """
    _require_id(tenant_id, "tenant_id")
    _require_id(sku, "sku")
    _require_id(source_warehouse_id, "source_warehouse_id")
    _require_id(destination_warehouse_id, "destination_warehouse_id")
    _validate_quantity(quantity)
    if source_warehouse_id == destination_warehouse_id:
        raise ValidationError(
            "same_warehouse",
            "source_warehouse_id and destination_warehouse_id must differ",
        )

    with db.transaction() as conn:
        # Existence checks are tenant-scoped. They give the same "not found"
        # for a missing resource and for another tenant's, so no data leaks.
        if not transfers_repo.warehouse_exists(conn, tenant_id, source_warehouse_id):
            raise NotFoundError("warehouse_not_found", "source warehouse not found")
        if not transfers_repo.warehouse_exists(
            conn, tenant_id, destination_warehouse_id
        ):
            raise NotFoundError(
                "warehouse_not_found", "destination warehouse not found"
            )
        if not transfers_repo.sku_exists(conn, tenant_id, sku):
            raise NotFoundError("sku_not_found", "sku not found")

        if not transfers_repo.decrement_stock(
            conn, tenant_id, source_warehouse_id, sku, quantity
        ):
            raise ConflictError(
                "insufficient_stock", "not enough stock at the source warehouse"
            )
        transfers_repo.increment_stock(
            conn, tenant_id, destination_warehouse_id, sku, quantity
        )
        row = transfers_repo.insert_transfer(
            conn,
            tenant_id,
            sku,
            source_warehouse_id,
            destination_warehouse_id,
            quantity,
        )
        transfer_id = str(row["id"])
        transfers_repo.insert_movement(
            conn,
            tenant_id,
            sku,
            source_warehouse_id,
            -quantity,
            transfers_repo.MOVEMENT_TRANSFER_OUT,
            transfer_id,
        )
        transfers_repo.insert_movement(
            conn,
            tenant_id,
            sku,
            destination_warehouse_id,
            quantity,
            transfers_repo.MOVEMENT_TRANSFER_IN,
            transfer_id,
        )

    # Only reached after transaction() has committed.
    for key in _stock_cache_keys(sku):
        cache.invalidate(key)

    return _to_transfer(row)


def _to_transfer(row: dict) -> Transfer:
    return Transfer(**{**row, "id": str(row["id"])})


def encode_cursor(created_at: datetime, transfer_id: str) -> str:
    """Opaque cursor: URL-safe base64 of ``"<created_at ISO>|<id>"``."""
    raw = f"{created_at.isoformat()}|{transfer_id}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Inverse of ``encode_cursor``; raises ``ValidationError`` if malformed."""
    try:
        raw = base64.b64decode(cursor, altchars=b"-_", validate=True).decode()
        created_part, id_part = raw.split("|")
        created_at = datetime.fromisoformat(created_part)
        transfer_id = str(uuid.UUID(id_part))
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError):
        raise ValidationError("invalid_cursor", "cursor is malformed") from None
    return created_at, transfer_id


def _validate_limit(limit) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if isinstance(limit, bool):
        raise ValidationError("invalid_limit", "limit must be an integer")
    if isinstance(limit, str):
        try:
            limit = int(limit.strip())
        except ValueError:
            raise ValidationError("invalid_limit", "limit must be an integer") from None
    if not isinstance(limit, int):
        raise ValidationError("invalid_limit", "limit must be an integer")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValidationError(
            "invalid_limit", f"limit must be between 1 and {MAX_LIMIT}"
        )
    return limit


def list_transfers(
    tenant_id: str,
    limit: int | str | None = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page:
    """One page of the tenant's transfers, newest first (keyset pagination).

    ``next_cursor`` is None on the last page. It is found by fetching
    ``limit + 1`` rows.
    """
    _require_id(tenant_id, "tenant_id")
    limit = _validate_limit(limit)
    after = decode_cursor(cursor) if cursor else None

    with db.transaction() as conn:
        rows = transfers_repo.list_transfers(conn, tenant_id, limit + 1, after=after)

    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor(last["created_at"], str(last["id"]))
    items = [_to_transfer(r).model_dump(mode="json") for r in rows]
    return Page(items=items, next_cursor=next_cursor)
