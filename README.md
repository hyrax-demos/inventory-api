# inventory-api

Inventory and warehouse-stock API for the Hyrax Labs storefront. Tracks SKUs,
stock levels per warehouse, and exposes report-generation and bulk-import
endpoints for the operations team.

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

| Method | Path                  | Description                       |
| ------ | --------------------- | --------------------------------- |
| GET    | `/health`             | Liveness check                    |
| GET    | `/items/{sku}`        | Look up a single item             |
| GET    | `/items`              | Search items by warehouse / name  |
| POST   | `/reports/export`     | Generate a stock report           |
| POST   | `/reports/import`     | Bulk-import a stock snapshot       |
| POST   | `/admin/items/reset`  | Reset all stock to zero (internal)|
