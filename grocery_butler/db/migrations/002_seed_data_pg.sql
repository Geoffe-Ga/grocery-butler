-- Seed pantry staples and default preferences (PostgreSQL).
-- Uses ON CONFLICT DO NOTHING so re-running is safe.

INSERT INTO pantry_staples (ingredient, display_name, category)
VALUES ('salt', 'Salt', 'pantry_dry')
ON CONFLICT DO NOTHING;

INSERT INTO pantry_staples (ingredient, display_name, category)
VALUES ('black pepper', 'Black Pepper', 'pantry_dry')
ON CONFLICT DO NOTHING;

INSERT INTO pantry_staples (ingredient, display_name, category)
VALUES ('olive oil', 'Olive Oil', 'pantry_dry')
ON CONFLICT DO NOTHING;

INSERT INTO pantry_staples (ingredient, display_name, category)
VALUES ('vegetable oil', 'Vegetable Oil', 'pantry_dry')
ON CONFLICT DO NOTHING;

INSERT INTO pantry_staples (ingredient, display_name, category)
VALUES ('butter', 'Butter', 'dairy')
ON CONFLICT DO NOTHING;

INSERT INTO pantry_staples (ingredient, display_name, category)
VALUES ('garlic', 'Garlic', 'produce')
ON CONFLICT DO NOTHING;

INSERT INTO pantry_staples (ingredient, display_name, category)
VALUES ('onion', 'Onion', 'produce')
ON CONFLICT DO NOTHING;

INSERT INTO pantry_staples (ingredient, display_name, category)
VALUES ('sugar', 'Sugar', 'pantry_dry')
ON CONFLICT DO NOTHING;

INSERT INTO pantry_staples (ingredient, display_name, category)
VALUES ('flour', 'Flour', 'pantry_dry')
ON CONFLICT DO NOTHING;

INSERT INTO pantry_staples (ingredient, display_name, category)
VALUES ('soy sauce', 'Soy Sauce', 'pantry_dry')
ON CONFLICT DO NOTHING;

INSERT INTO preferences (key, value)
VALUES ('default_servings', '4')
ON CONFLICT DO NOTHING;

INSERT INTO preferences (key, value)
VALUES ('default_units', 'imperial')
ON CONFLICT DO NOTHING;
