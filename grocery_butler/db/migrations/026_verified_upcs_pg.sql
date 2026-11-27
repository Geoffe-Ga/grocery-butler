-- Migration 026: verified product codes, learned from receipts (PostgreSQL).
--
-- A household item can hold one product code (a UPC) that a member confirmed
-- on a receipt line, with who confirmed it and when (ADR 0023). A receipt
-- line keeps the code it was submitted with. No code is ever inferred.

ALTER TABLE household_items ADD COLUMN verified_upc TEXT;
ALTER TABLE household_items ADD COLUMN upc_verified_by TEXT;
ALTER TABLE household_items ADD COLUMN upc_verified_at TEXT;

ALTER TABLE receipt_lines ADD COLUMN upc TEXT;
