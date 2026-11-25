-- Migration 009: archive the retired Safeway tables (SQLite).
-- ADR 0001 removed the private Safeway integration. product_mapping (the
-- Safeway product cache) and order_submissions (the duplicate-order
-- ledger) have no reader or writer left. They are renamed rather than
-- dropped so any historical rows survive for audit or export, e.g.
--   sqlite3 mealbot.db '.dump archived_order_submissions'
-- SQLite can't rename an index, so each is recreated under the new name.

ALTER TABLE product_mapping RENAME TO archived_product_mapping;
ALTER TABLE order_submissions RENAME TO archived_order_submissions;

DROP INDEX IF EXISTS idx_product_mapping_ingredient;
CREATE INDEX IF NOT EXISTS idx_archived_product_mapping_ingredient
    ON archived_product_mapping(ingredient_description);
DROP INDEX IF EXISTS idx_order_submissions_fingerprint;
CREATE INDEX IF NOT EXISTS idx_archived_order_submissions_fingerprint
    ON archived_order_submissions(cart_fingerprint, created_at);
