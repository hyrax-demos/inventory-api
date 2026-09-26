-- 0001: stock transfers between warehouses.
--
-- Plain PostgreSQL. Apply with:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -1 -f migrations/0001_create_transfers.up.sql
-- Migrations are forward-only: there is no down file, because reverting would
-- mean dropping the transfers table and movements columns (data loss). Undo a
-- change with a new, reviewed forward migration once no code references it.
--
-- The existing tables (items = per-warehouse stock rows, movements) key on
-- text tenant_id / sku / warehouse_id and there is no warehouses or skus
-- table, so transfers uses the same text columns and has no FKs to them.

CREATE TABLE IF NOT EXISTS transfers (
    id                       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                TEXT        NOT NULL,
    sku                      TEXT        NOT NULL,
    source_warehouse_id      TEXT        NOT NULL,
    destination_warehouse_id TEXT        NOT NULL,
    quantity                 INTEGER     NOT NULL,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT transfers_quantity_positive CHECK (quantity > 0),
    CONSTRAINT transfers_distinct_warehouses
        CHECK (source_warehouse_id <> destination_warehouse_id)
);

-- Newest-first, keyset-paginated listing per tenant.
CREATE INDEX IF NOT EXISTS transfers_tenant_created_id_idx
    ON transfers (tenant_id, created_at DESC, id DESC);

-- The destination-side upsert (INSERT ... ON CONFLICT) needs one stock row
-- per (tenant, warehouse, sku).
CREATE UNIQUE INDEX IF NOT EXISTS items_tenant_warehouse_sku_uidx
    ON items (tenant_id, warehouse_id, sku);

-- Movements: record why a row exists and which transfer it came from.
-- Both columns are nullable so existing rows and writers are unaffected.
ALTER TABLE movements ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE movements ADD COLUMN IF NOT EXISTS transfer_id UUID
    REFERENCES transfers (id);
ALTER TABLE movements DROP CONSTRAINT IF EXISTS movements_reason_check;
ALTER TABLE movements ADD CONSTRAINT movements_reason_check
    CHECK (reason IS NULL OR reason IN ('transfer_in', 'transfer_out'));
CREATE INDEX IF NOT EXISTS movements_transfer_id_idx
    ON movements (transfer_id);
