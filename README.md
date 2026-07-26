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

## Usage

Once the local development environment is set up (see above), start the API server in development mode with live-reload enabled. The server will be available at `http://localhost:8000` by default, and interactive API documentation is served at `http://localhost:8000/docs`.

```bash
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

Admin and sync endpoints require the `X-Admin-Token` header.
