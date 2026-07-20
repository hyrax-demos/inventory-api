# reserved_value query joins items without tenant_id — can price reservations using another tenant's item prices

**Tool:** `database`
**Severity:** high
**Category:** architecture
**Location:** `app/routes/reports.py:47`

## What's wrong

The `reserved_value` report joins `reservations r` to `items i` on `i.sku = r.sku AND i.warehouse_id = r.warehouse_id` but does NOT include `AND i.tenant_id = r.tenant_id`. The WHERE clause only filters `r.tenant_id = %s`. If two tenants share a SKU+warehouse_id combination, the JOIN can match item rows from the wrong tenant and price Tenant A's reservations with Tenant B's prices. This produces incorrect financial reporting in a multi-tenant system.
