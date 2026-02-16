"""SQLite data access layer for recipes, pantry staples, and preferences.

Provides CRUD operations for all persistent data. Uses synchronous SQLite
with connection-per-operation pattern and WAL mode for concurrency.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from grocery_butler.db import get_connection, init_db
from grocery_butler.models import (
    BrandMatchType,
    BrandPreference,
    BrandPreferenceType,
    Ingredient,
    IngredientCategory,
    ParsedMeal,
)

if TYPE_CHECKING:
    import sqlite3


_ARTICLE_RE = re.compile(r"^(a|an|the)\s+", re.IGNORECASE)
_POSSESSIVE_RE = re.compile(r"'s\b")
_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_recipe_name(name: str) -> str:
    """Normalize a recipe name for consistent lookup.

    Lowercases, strips articles, normalizes possessives,
    removes punctuation, and collapses whitespace.

    Args:
        name: Raw recipe name.

    Returns:
        Normalized name string.
    """
    result = name.lower().strip()
    result = _ARTICLE_RE.sub("", result)
    result = _POSSESSIVE_RE.sub("s", result)
    result = _PUNCTUATION_RE.sub("", result)
    result = _WHITESPACE_RE.sub(" ", result).strip()
    return result


def _row_to_parsed_meal(
    recipe_row: sqlite3.Row,
    ingredient_rows: list[sqlite3.Row],
) -> ParsedMeal:
    """Convert database rows to a ParsedMeal model.

    Args:
        recipe_row: Row from the recipes table.
        ingredient_rows: Rows from recipe_ingredients for this recipe.

    Returns:
        ParsedMeal model instance.
    """
    purchase = []
    pantry = []
    for row in ingredient_rows:
        ing = Ingredient(
            ingredient=row["ingredient"],
            quantity=row["quantity"],
            unit=row["unit"],
            category=IngredientCategory(row["category"]),
            notes=row["notes"] or "",
            is_pantry_item=bool(row["is_pantry_item"]),
        )
        if ing.is_pantry_item:
            pantry.append(ing)
        else:
            purchase.append(ing)
    return ParsedMeal(
        name=recipe_row["display_name"],
        servings=recipe_row["default_servings"],
        known_recipe=True,
        needs_confirmation=False,
        purchase_items=purchase,
        pantry_items=pantry,
    )


class RecipeStore:
    """SQLite data access layer for recipes and pantry data."""

    def __init__(self, db_path: str) -> None:
        """Initialize store and ensure schema exists.

        Args:
            db_path: Path to the SQLite database file.
        """
        self._db_path = db_path
        init_db(db_path)

    def _connect(self) -> sqlite3.Connection:
        """Create a new database connection.

        Returns:
            Configured sqlite3.Connection.
        """
        return get_connection(self._db_path)

    # ------------------------------------------------------------------
    # Recipe CRUD
    # ------------------------------------------------------------------

    def save_recipe(self, meal: ParsedMeal) -> int:
        """Save a parsed meal as a recipe.

        Args:
            meal: Parsed meal to persist.

        Returns:
            The new recipe's database ID.
        """
        normalized = normalize_recipe_name(meal.name)
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO recipes (name, display_name, default_servings)"
                " VALUES (?, ?, ?)",
                (normalized, meal.name, meal.servings),
            )
            recipe_id = cursor.lastrowid
            if recipe_id is None:  # pragma: no cover
                msg = "INSERT did not return a row ID"
                raise RuntimeError(msg)
            self._insert_ingredients(conn, recipe_id, meal)
            conn.commit()
            return recipe_id
        finally:
            conn.close()

    def _insert_ingredients(
        self,
        conn: sqlite3.Connection,
        recipe_id: int,
        meal: ParsedMeal,
    ) -> None:
        """Insert all ingredients for a recipe.

        Args:
            conn: Active database connection.
            recipe_id: ID of the parent recipe.
            meal: Meal containing ingredient lists.
        """
        all_items = [
            *[(ing, False) for ing in meal.purchase_items],
            *[(ing, True) for ing in meal.pantry_items],
        ]
        servings = meal.servings or 1
        for ing, is_pantry in all_items:
            conn.execute(
                "INSERT INTO recipe_ingredients"
                " (recipe_id, ingredient, quantity, unit, category,"
                "  is_pantry_item, notes, quantity_per_serving)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    recipe_id,
                    ing.ingredient,
                    ing.quantity,
                    ing.unit,
                    ing.category.value,
                    is_pantry,
                    ing.notes or None,
                    ing.quantity / servings,
                ),
            )

    def get_recipe(self, name: str) -> ParsedMeal | None:
        """Look up a recipe by normalized name.

        Args:
            name: Recipe name (will be normalized).

        Returns:
            ParsedMeal if found, None otherwise.
        """
        normalized = normalize_recipe_name(name)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM recipes WHERE name = ?",
                (normalized,),
            ).fetchone()
            if row is None:
                return None
            ingredients = conn.execute(
                "SELECT * FROM recipe_ingredients WHERE recipe_id = ?",
                (row["id"],),
            ).fetchall()
            return _row_to_parsed_meal(row, ingredients)
        finally:
            conn.close()

    def get_recipe_by_id(self, recipe_id: int) -> ParsedMeal | None:
        """Look up a recipe by its database ID.

        Args:
            recipe_id: Recipe database ID.

        Returns:
            ParsedMeal if found, None otherwise.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM recipes WHERE id = ?",
                (recipe_id,),
            ).fetchone()
            if row is None:
                return None
            ingredients = conn.execute(
                "SELECT * FROM recipe_ingredients WHERE recipe_id = ?",
                (recipe_id,),
            ).fetchall()
            return _row_to_parsed_meal(row, ingredients)
        finally:
            conn.close()

    def update_recipe(self, recipe_id: int, meal: ParsedMeal) -> None:
        """Replace a recipe's ingredients entirely.

        Args:
            recipe_id: ID of recipe to update.
            meal: New meal data.
        """
        normalized = normalize_recipe_name(meal.name)
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE recipes SET name = ?, display_name = ?,"
                " default_servings = ?, updated_at = CURRENT_TIMESTAMP"
                " WHERE id = ?",
                (normalized, meal.name, meal.servings, recipe_id),
            )
            conn.execute(
                "DELETE FROM recipe_ingredients WHERE recipe_id = ?",
                (recipe_id,),
            )
            self._insert_ingredients(conn, recipe_id, meal)
            conn.commit()
        finally:
            conn.close()

    def delete_recipe(self, recipe_id: int) -> None:
        """Delete a recipe and its ingredients.

        Args:
            recipe_id: ID of recipe to delete.
        """
        conn = self._connect()
        try:
            conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
            conn.commit()
        finally:
            conn.close()

    def list_recipes(self) -> list[dict[str, object]]:
        """List all stored recipes with summary info.

        Returns:
            List of dicts with id, name, display_name, times_ordered.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, name, display_name, times_ordered"
                " FROM recipes ORDER BY name",
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def export_recipe_json(self, recipe_id: int) -> str | None:
        """Export a recipe as a JSON string for Claude context.

        Args:
            recipe_id: ID of recipe to export.

        Returns:
            JSON string or None if recipe not found.
        """
        meal = self.get_recipe_by_id(recipe_id)
        if meal is None:
            return None
        return json.dumps(meal.model_dump(), indent=2, default=str)

    def increment_times_ordered(self, recipe_id: int) -> None:
        """Increment the times_ordered counter for a recipe.

        Args:
            recipe_id: ID of recipe to increment.
        """
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE recipes SET times_ordered = times_ordered + 1 WHERE id = ?",
                (recipe_id,),
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Fuzzy recipe lookup
    # ------------------------------------------------------------------

    def find_recipe(self, query: str) -> ParsedMeal | None:
        """Find a recipe by exact or substring match.

        Tries exact normalized match first, then substring.

        Args:
            query: Search query string.

        Returns:
            Best matching ParsedMeal or None.
        """
        normalized = normalize_recipe_name(query)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM recipes WHERE name = ?",
                (normalized,),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM recipes WHERE name LIKE ?"
                    " ORDER BY times_ordered DESC LIMIT 1",
                    (f"%{normalized}%",),
                ).fetchone()
            if row is None:
                return None
            ingredients = conn.execute(
                "SELECT * FROM recipe_ingredients WHERE recipe_id = ?",
                (row["id"],),
            ).fetchall()
            return _row_to_parsed_meal(row, ingredients)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Pantry staples
    # ------------------------------------------------------------------

    def get_pantry_staples(self) -> list[dict[str, object]]:
        """Return all pantry staples with full details.

        Returns:
            List of dicts with id, ingredient, display_name, category.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, ingredient, display_name, category"
                " FROM pantry_staples ORDER BY ingredient",
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_pantry_staple_names(self) -> list[str]:
        """Return just the ingredient names of pantry staples.

        Returns:
            List of ingredient name strings.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT ingredient FROM pantry_staples ORDER BY ingredient",
            ).fetchall()
            return [row["ingredient"] for row in rows]
        finally:
            conn.close()

    def add_pantry_staple(self, ingredient: str, category: str) -> int:
        """Add a new pantry staple.

        Args:
            ingredient: Ingredient name (lowercased for storage).
            category: Category string.

        Returns:
            Database ID of the new staple.
        """
        display_name = ingredient.strip().title()
        ingredient_lower = ingredient.strip().lower()
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO pantry_staples"
                " (ingredient, display_name, category) VALUES (?, ?, ?)",
                (ingredient_lower, display_name, category),
            )
            conn.commit()
            row_id = cursor.lastrowid
            if row_id is None:  # pragma: no cover
                msg = "INSERT did not return a row ID"
                raise RuntimeError(msg)
            return row_id
        finally:
            conn.close()

    def remove_pantry_staple(self, staple_id: int) -> None:
        """Remove a pantry staple by ID.

        Args:
            staple_id: Database ID of staple to remove.
        """
        conn = self._connect()
        try:
            conn.execute("DELETE FROM pantry_staples WHERE id = ?", (staple_id,))
            conn.commit()
        finally:
            conn.close()

    def is_pantry_staple(self, ingredient: str) -> bool:
        """Check if an ingredient is a pantry staple.

        Args:
            ingredient: Ingredient name to check.

        Returns:
            True if the ingredient is a pantry staple.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM pantry_staples WHERE ingredient = ?",
                (ingredient.strip().lower(),),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    def get_preference(self, key: str) -> str | None:
        """Get a single preference value.

        Args:
            key: Preference key.

        Returns:
            Value string or None if not set.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM preferences WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()

    def set_preference(self, key: str, value: str) -> None:
        """Set a preference (upsert).

        Args:
            key: Preference key.
            value: Preference value.
        """
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO preferences (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()

    def get_all_preferences(self) -> dict[str, str]:
        """Return all preferences as a dict.

        Returns:
            Dict mapping preference keys to values.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT key, value FROM preferences ORDER BY key",
            ).fetchall()
            return {row["key"]: row["value"] for row in rows}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Brand preferences
    # ------------------------------------------------------------------

    def get_brand_preferences(self) -> list[BrandPreference]:
        """Return all brand preferences.

        Returns:
            List of BrandPreference models.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM brand_preferences ORDER BY match_target",
            ).fetchall()
            return [
                BrandPreference(
                    match_target=row["match_target"],
                    match_type=BrandMatchType(row["match_type"]),
                    brand=row["brand"],
                    preference_type=BrandPreferenceType(row["preference_type"]),
                    notes=row["notes"] or "",
                )
                for row in rows
            ]
        finally:
            conn.close()

    def add_brand_preference(self, pref: BrandPreference) -> int:
        """Add a brand preference.

        Args:
            pref: Brand preference to add.

        Returns:
            Database ID of the new preference.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO brand_preferences"
                " (match_target, match_type, brand, preference_type, notes)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    pref.match_target,
                    pref.match_type.value,
                    pref.brand,
                    pref.preference_type.value,
                    pref.notes or None,
                ),
            )
            conn.commit()
            row_id = cursor.lastrowid
            if row_id is None:  # pragma: no cover
                msg = "INSERT did not return a row ID"
                raise RuntimeError(msg)
            return row_id
        finally:
            conn.close()

    def remove_brand_preference(self, pref_id: int) -> None:
        """Remove a brand preference by ID.

        Args:
            pref_id: Database ID of preference to remove.
        """
        conn = self._connect()
        try:
            conn.execute("DELETE FROM brand_preferences WHERE id = ?", (pref_id,))
            conn.commit()
        finally:
            conn.close()

    def get_brands_for_ingredient(
        self, ingredient: str, category: str | None = None
    ) -> list[BrandPreference]:
        """Get brand preferences for an ingredient.

        Checks ingredient-level first, falls back to category-level.

        Args:
            ingredient: Ingredient name to look up.
            category: Optional category for fallback lookup.

        Returns:
            List of matching BrandPreference models.
        """
        conn = self._connect()
        try:
            # Ingredient-level preferences
            rows = conn.execute(
                "SELECT * FROM brand_preferences"
                " WHERE match_type = 'ingredient'"
                " AND match_target = ?",
                (ingredient.lower(),),
            ).fetchall()
            if rows:
                return self._rows_to_brand_prefs(rows)
            # Fall back to category-level
            if category is not None:
                rows = conn.execute(
                    "SELECT * FROM brand_preferences"
                    " WHERE match_type = 'category'"
                    " AND match_target = ?",
                    (category.lower(),),
                ).fetchall()
                return self._rows_to_brand_prefs(rows)
            return []
        finally:
            conn.close()

    @staticmethod
    def _rows_to_brand_prefs(
        rows: list[sqlite3.Row],
    ) -> list[BrandPreference]:
        """Convert brand preference rows to models.

        Args:
            rows: Database rows from brand_preferences table.

        Returns:
            List of BrandPreference models.
        """
        return [
            BrandPreference(
                match_target=row["match_target"],
                match_type=BrandMatchType(row["match_type"]),
                brand=row["brand"],
                preference_type=BrandPreferenceType(row["preference_type"]),
                notes=row["notes"] or "",
            )
            for row in rows
        ]
