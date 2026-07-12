-- Migration 007: product_mapping size/stock columns (SQLite).
-- Issue #71: ProductSearchService used to rehydrate cached rows with a
-- hardcoded size="" and a default in_stock=True because product_mapping
-- never stored the real values, pinning cached quantity calculations to 1
-- (unparseable_size) and preventing the substitution flow from ever
-- triggering for cached items. These columns let save_mapping/pin_mapping
-- persist the real product size and stock status so cache hits can be
-- re-verified and rehydrated accurately.

ALTER TABLE product_mapping ADD COLUMN safeway_product_size TEXT;
ALTER TABLE product_mapping ADD COLUMN safeway_in_stock BOOLEAN DEFAULT TRUE;
