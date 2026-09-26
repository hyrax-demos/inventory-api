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
| POST   | `/admin/items/bulk-adjust`      | Apply stock deltas in bulk           |
| POST   | `/admin/items/reset`            | Reset all stock to zero (internal)   |
| DELETE | `/admin/items/{item_id}`        | Delete a discontinued SKU            |
| POST   | `/admin/items/{item_id}/update` | Patch whitelisted item fields        |
| GET    | `/health`                       | Liveness check                       |
| GET    | `/items`                        | Search items (paginated)             |
| POST   | `/items/reserve`                | Reserve stock for an order           |
| GET    | `/items/{sku}`                  | Look up a single item                |
| PATCH  | `/items/{sku}`                  | Partially update `name`, `price`, `warehouse_id` |
| GET    | `/items/{sku}/stock`            | On-hand quantity (cached)            |
| POST   | `/reports/import`               | Bulk-import a stock snapshot          |
| GET    | `/reports/low-stock`            | Items at/below `threshold` (paginated: `limit`, `cursor`) |
| GET    | `/reports/reserved-value`       | Dollar value of reserved stock       |
| GET    | `/reports/today`                | Stock movements recorded today       |
| POST   | `/reservations/{order_id}/release` | Release a reservation             |
| POST   | `/sync/item/{sku}`              | Refresh price for one SKU            |
| POST   | `/sync/prices`                  | Sync prices from the provider feed   |

Admin and sync endpoints require the `X-Admin-Token` header.

### `PATCH /items/{sku}`

The JSON body may contain any of `name`, `price` (must be `>= 0`), and
`warehouse_id`. Only fields that are present and non-null are changed. An
empty body returns the item as-is. The response is the updated item. An
unknown SKU returns `404`.

Changing `price` also requires a valid `X-Admin-Token` header (the same token
as the admin and sync endpoints); without it the request is rejected with
`401` and nothing is changed. `name` and `warehouse_id` edits need only
`X-Tenant-Id`.

### `GET /reports/low-stock` pagination

| Query param | Default | Description                                            |
| ----------- | ------- | ------------------------------------------------------ |
| `threshold` | `10`    | Include items with `quantity <= threshold`             |
| `limit`     | `50`    | Page size (must be `>= 1`)                             |
| `cursor`    | —       | The `next_cursor` from the previous page; omit for page 1 |

Results are ordered by `quantity` then `id`, both ascending (most urgent first).
The response is `{"threshold", "items", "next_cursor"}`. `next_cursor` is
`null` on the last page. A malformed `cursor` returns `400`.
