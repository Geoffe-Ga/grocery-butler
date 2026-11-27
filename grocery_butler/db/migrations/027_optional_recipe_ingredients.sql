-- Migration 027: optional recipe ingredients (ADR 0025) (SQLite).
--
-- A recipe ingredient is required unless it is marked optional. Every row
-- that exists today becomes required, which is what it always meant. An
-- optional ingredient reaches a shopping plan only when a member includes
-- it for that plan.

ALTER TABLE recipe_ingredients ADD COLUMN is_optional BOOLEAN NOT NULL DEFAULT FALSE;
