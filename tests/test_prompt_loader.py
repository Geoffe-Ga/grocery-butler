"""Tests for grocery_butler.prompt_loader module."""

from __future__ import annotations

import pytest

from grocery_butler.prompt_loader import PROMPTS_DIR, load_prompt


class TestLoadPrompt:
    """Tests for the load_prompt function."""

    def test_load_meal_decomposition(self) -> None:
        """Test loading meal_decomposition template with all variables."""
        result = load_prompt(
            "meal_decomposition",
            default_servings="4",
            dietary_restrictions="none",
            pantry_staples="salt, pepper, olive oil",
            units="imperial",
            known_recipes="Chicken Tikka Masala, Tacos",
        )
        assert "chicken" in result.lower() or "servings" in result.lower()
        assert "4" in result
        assert "imperial" in result

    def test_load_ingredient_consolidation(self) -> None:
        """Test loading ingredient_consolidation template."""
        result = load_prompt(
            "ingredient_consolidation",
            pantry_staples="salt, pepper",
            restock_queue="soy sauce (out)",
            restock_items="butter (low)",
        )
        assert "salt" in result
        assert "soy sauce" in result

    def test_load_recipe_matching(self) -> None:
        """Test loading recipe_matching template."""
        result = load_prompt(
            "recipe_matching",
            query="tikka",
            recipe_list="Chicken Tikka Masala, Tacos, Caesar Salad",
        )
        assert "tikka" in result
        assert "Chicken Tikka Masala" in result

    def test_load_inventory_intent(self) -> None:
        """Test loading inventory_intent template."""
        result = load_prompt(
            "inventory_intent",
            user_message="we're out of milk and low on eggs",
            current_inventory="milk: on_hand, eggs: on_hand, butter: on_hand",
        )
        assert "milk" in result
        assert "eggs" in result

    def test_load_product_selection(self) -> None:
        """Test loading product_selection template with variables."""
        result = load_prompt(
            "product_selection",
            ingredient="whole milk",
            quantity="1.0",
            unit="gal",
            search_term="whole milk gallon",
            category="dairy",
            products_json="[]",
            brand_preferences="No preferences",
            price_sensitivity="moderate",
        )
        assert "whole milk" in result
        assert "moderate" in result

    def test_load_substitution_ranking(self) -> None:
        """Test loading substitution_ranking template with variables."""
        result = load_prompt(
            "substitution_ranking",
            ingredient="chicken thighs",
            quantity="2",
            unit="lb",
            search_term="boneless chicken thighs",
            alternatives_json="[]",
            brand_preferences="None",
        )
        assert "chicken thighs" in result
        assert "suitability" in result.lower()

    def test_load_brand_selection(self) -> None:
        """Test loading brand_selection template with variables."""
        result = load_prompt(
            "brand_selection",
            products_json="[]",
            avoided_brands="Great Value",
            preferred_brands="Organic Valley",
        )
        assert "Great Value" in result
        assert "Organic Valley" in result

    def test_missing_variable_raises_key_error(self) -> None:
        """Test that missing template variable raises KeyError."""
        with pytest.raises(KeyError):
            load_prompt("meal_decomposition", default_servings="4")

    def test_nonexistent_template_raises_file_not_found(self) -> None:
        """Test that nonexistent template raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="Prompt template not found"):
            load_prompt("this_does_not_exist")

    def test_prompts_dir_exists(self) -> None:
        """Test that the prompts directory exists."""
        assert PROMPTS_DIR.exists()
        assert PROMPTS_DIR.is_dir()

    def test_all_template_files_exist(self) -> None:
        """Test that all expected template files are present."""
        expected = [
            "meal_decomposition.txt",
            "ingredient_consolidation.txt",
            "recipe_matching.txt",
            "inventory_intent.txt",
            "product_selection.txt",
            "substitution_ranking.txt",
            "brand_selection.txt",
        ]
        for filename in expected:
            assert (PROMPTS_DIR / filename).exists(), f"Missing: {filename}"
