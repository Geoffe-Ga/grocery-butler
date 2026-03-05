-- Seed pantry staples and default preferences (SQLite).
-- Uses INSERT OR IGNORE so re-running is safe.

INSERT OR IGNORE INTO pantry_staples (ingredient, display_name, category)
VALUES ('salt', 'Salt', 'pantry_dry');

INSERT OR IGNORE INTO pantry_staples (ingredient, display_name, category)
VALUES ('black pepper', 'Black Pepper', 'pantry_dry');

INSERT OR IGNORE INTO pantry_staples (ingredient, display_name, category)
VALUES ('olive oil', 'Olive Oil', 'pantry_dry');

INSERT OR IGNORE INTO pantry_staples (ingredient, display_name, category)
VALUES ('vegetable oil', 'Vegetable Oil', 'pantry_dry');

INSERT OR IGNORE INTO pantry_staples (ingredient, display_name, category)
VALUES ('butter', 'Butter', 'dairy');

INSERT OR IGNORE INTO pantry_staples (ingredient, display_name, category)
VALUES ('garlic', 'Garlic', 'produce');

INSERT OR IGNORE INTO pantry_staples (ingredient, display_name, category)
VALUES ('onion', 'Onion', 'produce');

INSERT OR IGNORE INTO pantry_staples (ingredient, display_name, category)
VALUES ('sugar', 'Sugar', 'pantry_dry');

INSERT OR IGNORE INTO pantry_staples (ingredient, display_name, category)
VALUES ('flour', 'Flour', 'pantry_dry');

INSERT OR IGNORE INTO pantry_staples (ingredient, display_name, category)
VALUES ('soy sauce', 'Soy Sauce', 'pantry_dry');

INSERT OR IGNORE INTO preferences (key, value)
VALUES ('default_servings', '4');

INSERT OR IGNORE INTO preferences (key, value)
VALUES ('default_units', 'imperial');
