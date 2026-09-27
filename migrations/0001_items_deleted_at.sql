-- Soft-delete support for items.
-- NULL = live, non-NULL = soft-deleted at that time. Existing rows stay NULL (live).
ALTER TABLE items ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL DEFAULT NULL;
