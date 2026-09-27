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
| GET    | `/reports/low-stock.csv`        | Low-stock report as CSV (see below)  |
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

Admin and sync endpoints require the `X-Admin-Token` header.

### Low-stock CSV export

`GET /reports/low-stock.csv` returns the same rows as `GET /reports/low-stock`,
as CSV. Both endpoints use the same query, so tenant scoping, threshold filtering
and ordering are identical.

- **Tenant header:** `X-Tenant-Id` (required). Rows come only from this tenant.
- **Query parameter:** `threshold` (integer, default `10`). An item is included
  when `quantity <= threshold`. Rows are sorted by `quantity`, lowest first.
- **Response:** `200` with a `text/csv` Content-Type, UTF-8 encoded. The body
  starts with the header row `sku,name,warehouse_id,quantity`, followed by one
  row per low-stock item in that column order. If no items match, the body is
  just the header row.
- **Quoting:** fields are written with Python's `csv` module and quoted per
  RFC 4180. A field that contains a comma, a double quote, or a newline (CR/LF)
  is wrapped in double quotes, and each embedded double quote is doubled
  (`"` becomes `""`). Rows end with CRLF.
- **Errors:** these match the JSON endpoint exactly.
  - A missing `X-Tenant-Id` header returns `422` with FastAPI's validation
    error body (`{"detail": [{"type": "missing", "loc": ["header", "x-tenant-id"], ...}]}`).
  - A non-integer `threshold` also returns `422`.
  - An empty or whitespace-only tenant value is not rejected. It is treated
    as a tenant with no items, so the response contains only the header row.

Example:

```bash
curl -s -H "X-Tenant-Id: acme" \
  "http://localhost:8000/reports/low-stock.csv?threshold=5"
```

```csv
sku,name,warehouse_id,quantity
SKU-1042,"Bolt, ""M6"" x 20mm",wh-east,2
SKU-0007,Washer,wh-west,5
```
