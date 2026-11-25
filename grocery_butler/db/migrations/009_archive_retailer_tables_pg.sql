-- Migration 009: archive the retired Safeway tables (PostgreSQL).
-- ADR 0001 removed the private Safeway integration. product_mapping (the
-- Safeway product cache) and order_submissions (the duplicate-order
-- ledger) have no reader or writer left. They are renamed rather than
-- dropped so any historical rows survive for audit or export, e.g.
--   pg_dump --table=archived_order_submissions "$DATABASE_URL"
-- Their indexes are renamed to match.

ALTER TABLE IF EXISTS product_mapping RENAME TO archived_product_mapping;
ALTER TABLE IF EXISTS order_submissions RENAME TO archived_order_submissions;
ALTER INDEX IF EXISTS idx_product_mapping_ingredient
    RENAME TO idx_archived_product_mapping_ingredient;
ALTER INDEX IF EXISTS idx_order_submissions_fingerprint
    RENAME TO idx_archived_order_submissions_fingerprint;
