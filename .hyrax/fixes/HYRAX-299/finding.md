# Bulk stock import endpoint (POST /reports/import) has no authentication

**Tool:** `auth`
**Severity:** high
**Category:** security
**Location:** `app/routes/reports.py:62`

## What's wrong

The `/reports/import` endpoint (`import_snapshot`) is a write operation that bulk-overwrites stock quantities for any number of SKUs across a tenant. The entire `reports.py` router has no `Depends(require_admin)` guard — neither at the router level nor on the individual handler. Any caller who knows a valid `X-Tenant-ID` header can bulk-reset all stock levels for that tenant. The `admin.py` and the two sync endpoints correctly use `require_admin`, making this the only write endpoint without auth.
