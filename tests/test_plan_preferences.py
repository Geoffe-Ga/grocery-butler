"""Tests for how soft preferences are put on a plan line (issue #215)."""

from __future__ import annotations

from grocery_butler.models import SoftPreference, SoftPreferenceKind
from grocery_butler.plan_preferences import (
    AVOID_BRANDS,
    GENERIC,
    ORGANIC,
    PREFERRED_BRAND,
    PREFERRED_SIZE,
    line_preferences,
    unfiltered_phrases,
)


def _soft(kind: str, value: str = "", source: str = "preference") -> SoftPreference:
    """Build one soft preference."""
    return SoftPreference(
        kind=SoftPreferenceKind(kind), value=value, item_id=None, source=source
    )


class TestLinePreferences:
    """Every soft preference reaches the line, under a stable key."""

    def test_nothing_wanted_is_an_empty_mapping(self) -> None:
        """A line with no preferences carries no keys at all."""
        assert line_preferences([]) == {}

    def test_each_kind_gets_its_key(self) -> None:
        """Brand and size keep their old keys; the rest are new."""
        found = line_preferences(
            [
                _soft("brand", "Oatly"),
                _soft("size", "64 oz"),
                _soft("organic"),
                _soft("generic"),
                _soft("avoid_brand", "Kerrygold"),
            ]
        )

        assert found == {
            PREFERRED_BRAND: "Oatly",
            PREFERRED_SIZE: "64 oz",
            ORGANIC: True,
            GENERIC: True,
            AVOID_BRANDS: ["Kerrygold"],
        }

    def test_the_items_own_brand_and_size_win(self) -> None:
        """What was set on the item beats a rule that merely matches it."""
        found = line_preferences(
            [
                _soft("brand", "Aardvark", source="brand_rule"),
                _soft("brand", "Oatly", source="item"),
                _soft("size", "1 gal", source="preference"),
                _soft("size", "64 oz", source="item"),
            ]
        )

        assert (found[PREFERRED_BRAND], found[PREFERRED_SIZE]) == ("Oatly", "64 oz")

    def test_without_the_items_own_the_first_by_name_is_used(self) -> None:
        """Deterministic: the same rules always give the same line."""
        found = line_preferences(
            [_soft("brand", "Zed", "brand_rule"), _soft("brand", "Alpha", "brand_rule")]
        )

        assert found[PREFERRED_BRAND] == "Alpha"

    def test_avoided_brands_are_sorted_and_not_repeated(self) -> None:
        """Two rules naming the same brand list it once."""
        found = line_preferences(
            [
                _soft("avoid_brand", "Tillamook"),
                _soft("avoid_brand", "Kerrygold"),
                _soft("avoid_brand", "Kerrygold", source="brand_rule"),
            ]
        )

        assert found[AVOID_BRANDS] == ["Kerrygold", "Tillamook"]


class TestUnfilteredPhrases:
    """Words for the preferences no retailer filter can express."""

    def test_brand_size_and_organic_need_no_words_here(self) -> None:
        """They are carried as filters or shown as their own fields."""
        constraints = {PREFERRED_BRAND: "Oatly", PREFERRED_SIZE: "64 oz", ORGANIC: True}

        assert unfiltered_phrases(constraints) == []

    def test_generic_and_avoided_brands_are_worded(self) -> None:
        """Plain words a person can act on while shopping."""
        constraints = {GENERIC: True, AVOID_BRANDS: ["Kerrygold", "Tillamook"]}

        assert unfiltered_phrases(constraints) == [
            "prefer the store brand",
            "avoid Kerrygold, Tillamook",
        ]
