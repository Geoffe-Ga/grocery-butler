"""Claude-powered meal decomposition and recipe matching.

Parses meal names into structured ingredient lists using the Claude API.
Falls back to stored recipes when available and degrades gracefully
when no API client is configured.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from grocery_butler.claude_utils import extract_json_text
from grocery_butler.models import Ingredient, IngredientCategory, ParsedMeal, parse_unit
from grocery_butler.prompt_loader import load_prompt
from grocery_butler.recipe_store import RecipeStore, normalize_recipe_name

if TYPE_CHECKING:
    from grocery_butler.config import Config

logger = logging.getLogger(__name__)

_MATCH_CONFIDENCE_THRESHOLD = 0.6


def _build_stub_meal(name: str, servings: int) -> ParsedMeal:
    """Build a stub ParsedMeal for graceful degradation.

    Args:
        name: The meal name.
        servings: Number of servings.

    Returns:
        A ParsedMeal with needs_confirmation=True and empty item lists.
    """
    return ParsedMeal(
        name=name,
        servings=servings,
        known_recipe=False,
        needs_confirmation=True,
        purchase_items=[],
        pantry_items=[],
    )


def _parse_ingredient(data: dict[str, object]) -> Ingredient:
    """Parse a single ingredient dict into an Ingredient model.

    Args:
        data: Dictionary with ingredient fields.

    Returns:
        Validated Ingredient instance.
    """
    raw_quantity = data.get("quantity", 0)
    quantity = float(raw_quantity) if isinstance(raw_quantity, (int, float)) else 0.0
    return Ingredient(
        ingredient=str(data.get("ingredient", "")),
        quantity=quantity,
        unit=parse_unit(str(data.get("unit", ""))),
        category=IngredientCategory(str(data.get("category", "other"))),
        notes=str(data.get("notes", "")),
        is_pantry_item=bool(data.get("is_pantry_item", False)),
    )


def _parse_meal_from_dict(data: dict[str, object]) -> ParsedMeal:
    """Parse a single meal dict into a ParsedMeal model.

    Args:
        data: Dictionary with meal fields from Claude's JSON response.

    Returns:
        Validated ParsedMeal instance.
    """
    purchase_raw = data.get("purchase_items", [])
    pantry_raw = data.get("pantry_items", [])
    purchase_list: list[dict[str, object]] = (
        purchase_raw if isinstance(purchase_raw, list) else []
    )
    pantry_list: list[dict[str, object]] = (
        pantry_raw if isinstance(pantry_raw, list) else []
    )
    raw_servings = data.get("servings", 4)
    servings = int(raw_servings) if isinstance(raw_servings, (int, float)) else 4
    return ParsedMeal(
        name=str(data.get("name", "")),
        servings=servings,
        known_recipe=bool(data.get("known_recipe", False)),
        needs_confirmation=bool(data.get("needs_confirmation", True)),
        purchase_items=[_parse_ingredient(i) for i in purchase_list],
        pantry_items=[_parse_ingredient(i) for i in pantry_list],
    )


def _scale_ingredients(
    items: list[Ingredient],
    original_servings: int,
    target_servings: int,
) -> list[Ingredient]:
    """Scale ingredient quantities for a different serving size.

    Args:
        items: Original ingredient list.
        original_servings: The recipe's default serving count.
        target_servings: Desired serving count.

    Returns:
        New list with scaled quantities.
    """
    if original_servings <= 0 or target_servings <= 0:
        return list(items)
    ratio = target_servings / original_servings
    return [
        item.model_copy(
            update={"quantity": round(item.quantity * ratio, 2)},
        )
        for item in items
    ]


class MealParser:
    """Parses meal names into structured ingredient lists.

    Uses stored recipes when available, Claude API for fuzzy matching
    and decomposition of unknown meals, and stub data as a fallback
    when no API client is configured.
    """

    def __init__(
        self,
        recipe_store: RecipeStore,
        anthropic_client: Any = None,
        config: Config | None = None,
    ) -> None:
        """Initialize the meal parser with dependencies.

        Args:
            recipe_store: Data access layer for stored recipes.
            anthropic_client: Optional Anthropic SDK client for Claude calls.
            config: Optional application configuration.
        """
        self._store = recipe_store
        self._client = anthropic_client
        self._config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse_meals(
        self,
        meal_names: list[str],
        servings: int | None = None,
    ) -> list[ParsedMeal]:
        """Parse a list of meal names into structured ingredient lists.

        For each meal name:
        1. Normalize and look up in stored recipes.
        2. If not found, try Claude fuzzy matching against stored recipes.
        3. If still not found, call Claude to decompose the meal.
        4. Adjust serving sizes when requested.

        Args:
            meal_names: List of meal name strings to parse.
            servings: Optional override for the number of servings.

        Returns:
            List of ParsedMeal objects, one per input meal name.
        """
        if not meal_names:
            return []

        default_servings = self._get_default_servings()
        target_servings = servings if servings is not None else default_servings
        results: list[ParsedMeal] = []

        for name in meal_names:
            meal = self._resolve_single_meal(name, target_servings)
            results.append(meal)

        return results

    def save_parsed_meal(self, meal: ParsedMeal) -> None:
        """Save a parsed meal to the recipe store.

        Args:
            meal: The parsed meal to persist.
        """
        self._store.save_recipe(meal)

    # ------------------------------------------------------------------
    # Internal resolution pipeline
    # ------------------------------------------------------------------

    def _resolve_single_meal(
        self,
        name: str,
        target_servings: int,
    ) -> ParsedMeal:
        """Resolve a single meal name through the lookup pipeline.

        Args:
            name: Raw meal name string.
            target_servings: Desired number of servings.

        Returns:
            Resolved ParsedMeal.
        """
        # Step 1: Direct lookup in recipe store
        stored = self._store.find_recipe(name)
        if stored is not None:
            return self._adjust_servings(stored, target_servings)

        # Step 2: Claude fuzzy matching against stored recipes
        matched = self._try_fuzzy_match(name)
        if matched is not None:
            return self._adjust_servings(matched, target_servings)

        # Step 3: Claude meal decomposition
        return self._decompose_meal(name, target_servings)

    def _try_fuzzy_match(self, name: str) -> ParsedMeal | None:
        """Attempt Claude-powered fuzzy matching against stored recipes.

        Args:
            name: Meal name to match.

        Returns:
            Matched ParsedMeal or None if no match found.
        """
        if self._client is None:
            return None

        recipes = self._store.list_recipes()
        if not recipes:
            return None

        recipe_list = "\n".join(str(r["display_name"]) for r in recipes)
        prompt = load_prompt(
            "recipe_matching",
            query=name,
            recipe_list=recipe_list,
        )

        response_text = self._call_claude(prompt)
        if response_text is None:
            return None

        return self._parse_fuzzy_match_response(response_text)

    def _parse_fuzzy_match_response(
        self,
        response_text: str,
    ) -> ParsedMeal | None:
        """Parse Claude's fuzzy match response and look up the matched recipe.

        Args:
            response_text: Raw JSON response from Claude.

        Returns:
            Matched ParsedMeal or None.
        """
        try:
            data = json.loads(extract_json_text(response_text))
        except (json.JSONDecodeError, ValueError):
            logger.warning("Failed to parse fuzzy match response")
            return None

        if not isinstance(data, dict):
            return None

        match_name = data.get("match")
        confidence = float(data.get("confidence", 0))

        if match_name is None or confidence < _MATCH_CONFIDENCE_THRESHOLD:
            return None

        return self._store.find_recipe(str(match_name))

    def _decompose_meal(
        self,
        name: str,
        target_servings: int,
    ) -> ParsedMeal:
        """Decompose an unknown meal using Claude.

        Args:
            name: Meal name to decompose.
            target_servings: Desired number of servings.

        Returns:
            ParsedMeal with needs_confirmation=True, or a stub if no client.
        """
        if self._client is None:
            return _build_stub_meal(name, target_servings)

        prompt = self._build_decomposition_prompt(name, target_servings)
        response_text = self._call_claude(prompt)
        if response_text is None:
            return _build_stub_meal(name, target_servings)

        parsed = self._parse_decomposition_response(response_text, name)
        if parsed is not None:
            return parsed

        # Retry once on parse failure
        retry_text = self._retry_claude(prompt, response_text)
        if retry_text is None:
            return _build_stub_meal(name, target_servings)

        retried = self._parse_decomposition_response(retry_text, name)
        if retried is not None:
            return retried

        return _build_stub_meal(name, target_servings)

    # ------------------------------------------------------------------
    # Claude API helpers
    # ------------------------------------------------------------------

    def _call_claude(self, prompt: str) -> str | None:
        """Send a prompt to Claude and return the text response.

        Args:
            prompt: The formatted prompt string.

        Returns:
            Response text or None on failure.
        """
        try:
            response = self._client.messages.create(
                model=self._get_model(),
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            return str(response.content[0].text)
        except Exception:
            logger.exception("Claude API call failed")
            return None

    def _retry_claude(
        self,
        original_prompt: str,
        bad_response: str,
    ) -> str | None:
        """Retry a Claude call after invalid JSON.

        Args:
            original_prompt: The original prompt that produced bad output.
            bad_response: The invalid response text.

        Returns:
            New response text or None on failure.
        """
        try:
            response = self._client.messages.create(
                model=self._get_model(),
                max_tokens=4096,
                messages=[
                    {"role": "user", "content": original_prompt},
                    {"role": "assistant", "content": bad_response},
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was not valid JSON. "
                            "Please return ONLY valid JSON with no "
                            "markdown fences or explanation."
                        ),
                    },
                ],
            )
            return str(response.content[0].text)
        except Exception:
            logger.exception("Claude retry call failed")
            return None

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_decomposition_response(
        self,
        response_text: str,
        meal_name: str,
    ) -> ParsedMeal | None:
        """Parse Claude's decomposition response into a ParsedMeal.

        Args:
            response_text: Raw JSON response from Claude.
            meal_name: Original meal name for fallback.

        Returns:
            ParsedMeal or None if parsing fails.
        """
        try:
            cleaned = extract_json_text(response_text)
            data = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Failed to parse decomposition response")
            return None

        return self._extract_meal_from_data(data, meal_name)

    @staticmethod
    def _extract_meal_from_data(
        data: object,
        meal_name: str,
    ) -> ParsedMeal | None:
        """Extract a ParsedMeal from parsed JSON data.

        Handles both array and single-object responses from Claude.

        Args:
            data: Parsed JSON data (list or dict).
            meal_name: Original meal name for matching.

        Returns:
            ParsedMeal or None if extraction fails.
        """
        normalized_query = normalize_recipe_name(meal_name)

        if isinstance(data, list):
            # Find the meal matching our query
            for item in data:
                if not isinstance(item, dict):
                    continue
                item_name = str(item.get("name", ""))
                if normalize_recipe_name(item_name) == normalized_query:
                    return _parse_meal_from_dict(item)
            # If no exact match, return the first item
            if data and isinstance(data[0], dict):
                return _parse_meal_from_dict(data[0])
            return None

        if isinstance(data, dict):
            return _parse_meal_from_dict(data)

        return None

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    def _build_decomposition_prompt(
        self,
        meal_name: str,
        target_servings: int,
    ) -> str:
        """Build the meal decomposition prompt.

        Args:
            meal_name: Name of the meal to decompose.
            target_servings: Desired number of servings.

        Returns:
            Formatted prompt string.
        """
        pantry_names = self._store.get_pantry_staple_names()
        recipes = self._store.list_recipes()
        known_recipe_names = [str(r["display_name"]) for r in recipes]
        dietary = self._get_dietary_restrictions()
        units = self._get_units()

        prompt = load_prompt(
            "meal_decomposition",
            default_servings=str(target_servings),
            dietary_restrictions=dietary if dietary else "None",
            pantry_staples=", ".join(pantry_names) if pantry_names else "None",
            units=units,
            known_recipes=(
                ", ".join(known_recipe_names) if known_recipe_names else "None"
            ),
        )
        return f"{prompt}\n\nMeals to decompose:\n- {meal_name}"

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    def _get_default_servings(self) -> int:
        """Get the default serving count from config or store.

        Returns:
            Default number of servings.
        """
        if self._config is not None:
            return self._config.default_servings
        pref = self._store.get_preference("default_servings")
        if pref is not None:
            try:
                return int(pref)
            except ValueError:
                pass
        return 4

    def _get_dietary_restrictions(self) -> str:
        """Get dietary restrictions from stored preferences.

        Returns:
            Dietary restrictions string or empty string.
        """
        return self._store.get_preference("dietary_restrictions") or ""

    def _get_units(self) -> str:
        """Get measurement units from config or store.

        Returns:
            Units string (e.g. 'imperial' or 'metric').
        """
        if self._config is not None:
            return self._config.default_units
        return self._store.get_preference("default_units") or "imperial"

    def _get_model(self) -> str:
        """Get the Claude model name.

        Returns:
            Model identifier string.
        """
        return "claude-sonnet-4-20250514"

    @staticmethod
    def _adjust_servings(
        meal: ParsedMeal,
        target_servings: int,
    ) -> ParsedMeal:
        """Adjust a meal's ingredient quantities for a target serving size.

        Args:
            meal: Original parsed meal.
            target_servings: Desired number of servings.

        Returns:
            ParsedMeal with scaled quantities (or original if no change).
        """
        if meal.servings == target_servings:
            return meal
        return ParsedMeal(
            name=meal.name,
            servings=target_servings,
            known_recipe=meal.known_recipe,
            needs_confirmation=meal.needs_confirmation,
            purchase_items=_scale_ingredients(
                meal.purchase_items,
                meal.servings,
                target_servings,
            ),
            pantry_items=_scale_ingredients(
                meal.pantry_items,
                meal.servings,
                target_servings,
            ),
        )
