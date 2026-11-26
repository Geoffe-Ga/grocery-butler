-- Migration 020: staple policies and par levels (ADR 0017, backlog P1.7) (PostgreSQL).
-- Every existing pantry staple gets the policy "replenish when low", which is
-- what the flat staple list meant. An item that already has a policy keeps it.
-- The three new columns are optional: a target amount to keep, and a hint for
-- how often the item is usually bought.

ALTER TABLE household_items ADD COLUMN IF NOT EXISTS target_quantity DOUBLE PRECISION;
ALTER TABLE household_items ADD COLUMN IF NOT EXISTS target_unit TEXT;
ALTER TABLE household_items ADD COLUMN IF NOT EXISTS cadence_days INTEGER;

UPDATE household_items
   SET staple_policy = 'replenish_when_low'
 WHERE staple_policy IS NULL
   AND merged_into_id IS NULL
   AND (
        id IN (SELECT item_id FROM pantry_staples WHERE item_id IS NOT NULL)
     OR id IN (
            SELECT merged_into_id FROM household_items
             WHERE merged_into_id IS NOT NULL
               AND id IN (
                    SELECT item_id FROM pantry_staples WHERE item_id IS NOT NULL
               )
        )
   );
