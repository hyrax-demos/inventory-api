# inventory-api

Inventory and warehouse-stock API for the Hyrax Labs storefront. Tracks SKUs,
per-warehouse stock levels, stock reservations, and exposes report-generation
and bulk-import endpoints for the operations team. All data is tenant-scoped
via the `X-Tenant-Id` header.

## Stack

- Python + FastAPI
- PostgreSQL via `psycopg2`

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in DB + secrets
uvicorn app.main:app --reload
```

## Endpoints

| Method | Path                            | Description                          |
| ------ | ------------------------------- | ------------------------------------ |
| GET    | `/health`                       | Liveness check                       |
| GET    | `/items/{sku}`                  | Look up a single item                |
| GET    | `/items`                        | Search items (paginated)             |
| GET    | `/items/{sku}/stock`            | On-hand quantity (cached)            |
| POST   | `/items/reserve`                | Reserve stock for an order           |
| GET    | `/reports/low-stock`            | Items at/below reorder threshold     |
| GET    | `/reports/today`                | Stock movements recorded today       |
| GET    | `/reports/reserved-value`       | Dollar value of reserved stock       |
| POST   | `/reports/import`               | Bulk-import a stock snapshot          |
| POST   | `/admin/items/reset`            | Reset all stock to zero (internal)   |
| DELETE | `/admin/items/{item_id}`        | Delete a discontinued SKU            |
| POST   | `/admin/items/{item_id}/update` | Patch whitelisted item fields        |
| POST   | `/admin/items/bulk-adjust`      | Apply stock deltas in bulk           |
| POST   | `/sync/prices`                  | Sync prices from the provider feed   |
| POST   | `/sync/item/{sku}`              | Refresh price for one SKU            |
| POST   | `/reservations/{order_id}/release` | Release a reservation             |
| POST   | `/transfers`                    | Move stock between warehouses        |
| GET    | `/transfers`                    | List transfers (newest first, paginated) |

Admin and sync endpoints require the `X-Admin-Token` header.

## Transfers

A transfer moves `quantity` units of one SKU from a source warehouse to a
destination warehouse, both belonging to the caller's tenant.

Both endpoints are tenant-scoped the same way as the rest of the API. The
tenant comes **only** from the `X-Tenant-Id` header, never from the body or
the query string, so a `tenant_id` sent in the body is ignored. If the header
is missing, FastAPI's standard 422 is returned. If it is empty, the response
is `400 {"detail": "missing tenant"}`.

Errors raised by transfer logic use this envelope, with a stable
machine-readable `code`:

```json
{"detail": "human-readable message", "code": "machine_code"}
```

### `POST /transfers`

Creates a transfer.

Request body (JSON object; fields other than these four are ignored):

| Field                      | Type    | Constraints                                   |
| -------------------------- | ------- | --------------------------------------------- |
| `sku`                      | string  | Required, non-blank                           |
| `source_warehouse_id`      | string  | Required, non-blank                           |
| `destination_warehouse_id` | string  | Required, non-blank, must differ from source  |
| `quantity`                 | integer | Required, a JSON integer `> 0` (no floats, strings or booleans) |

```bash
curl -X POST localhost:8000/transfers \
  -H 'X-Tenant-Id: tenant-a' -H 'Content-Type: application/json' \
  -d '{"sku": "WIDGET", "source_warehouse_id": "wA", "destination_warehouse_id": "wB", "quantity": 4}'
```

All of the following happen in **one database transaction**, so they either
all commit or none do:

1. The source stock row is decremented with a conditional update that only
   succeeds if it holds at least `quantity`. Concurrent transfers therefore
   can't drive stock negative.
2. The destination stock row is incremented. It is created if the SKU had no
   row in that warehouse yet.
3. A `transfers` row is inserted.
4. Two `movements` rows are inserted, both carrying the transfer's id in
   `transfer_id`: one with `reason` `transfer_out` and a negative `delta` for
   the source, and one with `reason` `transfer_in` and a positive `delta` for
   the destination.

The stock cache entry for the SKU (`stock:{sku}`) is invalidated only after
the transaction commits. A failed or rolled-back transfer leaves stock,
transfers, movements and the cache unchanged.

**201 Created**

```json
{
  "id": "3f1c2a9e-8b7d-4e2f-9a61-0c5d4b3e2a10",
  "sku": "WIDGET",
  "source_warehouse_id": "wA",
  "destination_warehouse_id": "wB",
  "quantity": 4,
  "created_at": "2024-06-01T12:34:56.789012Z"
}
```

`tenant_id` is not included in the response.

**Errors**, in the order the checks run:

| Status | `code`                | When                                                                  |
| ------ | --------------------- | --------------------------------------------------------------------- |
| 400    | `invalid_body`        | Body is not valid JSON, or is not a JSON object                       |
| 400    | `invalid_field`       | `sku`, `source_warehouse_id` or `destination_warehouse_id` is missing, blank or not a string |
| 400    | `invalid_quantity`    | `quantity` is missing or not an integer, or is `<= 0`                 |
| 400    | `same_warehouse`      | `source_warehouse_id` equals `destination_warehouse_id`               |
| 404    | `warehouse_not_found` | Source or destination warehouse doesn't exist in the caller's tenant  |
| 404    | `sku_not_found`       | The SKU doesn't exist in the caller's tenant                          |
| 409    | `insufficient_stock`  | The source holds less than `quantity` of the SKU, or has no stock row for it at all |

A warehouse or SKU that belongs to another tenant returns the same 404 as one
that doesn't exist, so other tenants' data is never revealed.

### `GET /transfers`

Lists the caller's tenant's transfers, newest first (`created_at` descending,
ties broken by `id` descending). It uses keyset pagination.

| Query param | Type    | Default | Constraints                                  |
| ----------- | ------- | ------- | -------------------------------------------- |
| `limit`     | integer | `20`    | `1`–`100`                                    |
| `cursor`    | string  | none    | Opaque; use a `next_cursor` from an earlier response as-is |

**200 OK**

```json
{
  "items": [
    {
      "id": "3f1c2a9e-8b7d-4e2f-9a61-0c5d4b3e2a10",
      "sku": "WIDGET",
      "source_warehouse_id": "wA",
      "destination_warehouse_id": "wB",
      "quantity": 4,
      "created_at": "2024-06-01T12:34:56.789012Z"
    }
  ],
  "next_cursor": "MjAyNC0wNi0wMVQxMjozNDo1Ni43ODkwMTIrMDA6MDB8M2YxYzJhOWUtOGI3ZC00ZTJmLTlhNjEtMGM1ZDRiM2UyYTEw"
}
```

To page through, repeat the request with `cursor=<next_cursor>` (keeping the
same `limit`) until `next_cursor` is `null`, which marks the last page:

```bash
curl -H 'X-Tenant-Id: tenant-a' 'localhost:8000/transfers?limit=50'
curl -H 'X-Tenant-Id: tenant-a' 'localhost:8000/transfers?limit=50&cursor=MjAy...'
```

Clients should treat the cursor as opaque. A cursor only filters within the
caller's own tenant, so another tenant's cursor can never expose rows outside
yours.

**Errors**

| Status | `code`           | When                                                |
| ------ | ---------------- | --------------------------------------------------- |
| 400    | `invalid_limit`  | `limit` is not an integer, or is `< 1` or `> 100`   |
| 400    | `invalid_cursor` | `cursor` is malformed                               |
