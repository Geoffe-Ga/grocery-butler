-- Migration 025: delete rows whose ingredient name is blank (SQLite).
--
-- Ingredient and inventory names have been refused when blank since #198,
-- but a database older than that can still hold such rows. A nameless row
-- can't be shown, matched or bought, and was already skipped on every read.
-- Geoff chose on 2026-11-26 to delete them rather than keep skipping them.
--
-- "Blank" means empty or only spaces, tabs or line breaks, the same rule
-- the models apply. Nothing references these rows: a blank name never
-- became a household item, so their item_id is NULL.
--
-- Forward-only. Take a backup first (docs/operations/data-lifecycle.md).

DELETE FROM recipe_ingredients
WHERE TRIM(REPLACE(REPLACE(REPLACE(ingredient, char(9), ''), char(10), ''), char(13), '')) = '';

DELETE FROM household_inventory
WHERE TRIM(REPLACE(REPLACE(REPLACE(ingredient, char(9), ''), char(10), ''), char(13), '')) = '';

DELETE FROM pantry_staples
WHERE TRIM(REPLACE(REPLACE(REPLACE(ingredient, char(9), ''), char(10), ''), char(13), '')) = '';
