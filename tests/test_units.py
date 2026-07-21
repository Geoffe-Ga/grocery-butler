"""Tests for grocery_butler.units module.

Covers the unit-dimension classifier, cross-unit conversion, and the
strict product-size parser introduced to fix issue #59 (quantity
calculations that ignored units, causing gross over/under-ordering).
"""

from __future__ import annotations

import pytest

# The module itself (rather than a `from ... import group_key`) is imported
# for TestGroupKey below: during the RED-first phase of issue #80,
# `group_key` did not exist yet, and a direct name import would have raised
# ImportError at collection time, failing every test in this file instead
# of just the new ones. The module-attribute pattern is kept for symmetry.
from grocery_butler import units
from grocery_butler.models import Unit
from grocery_butler.units import Dimension, convert, dimension_of, parse_size

# ------------------------------------------------------------------
# Tests: dimension_of
# ------------------------------------------------------------------


class TestDimensionOf:
    """Tests for dimension_of."""

    @pytest.mark.parametrize(
        "unit",
        [Unit.G, Unit.KG, Unit.OZ, Unit.LB],
    )
    def test_mass_units_return_mass_dimension(self, unit: Unit) -> None:
        """Test mass units classify as Dimension.MASS."""
        assert dimension_of(unit) == Dimension.MASS

    @pytest.mark.parametrize(
        "unit",
        [Unit.ML, Unit.L, Unit.TSP, Unit.TBSP, Unit.FL_OZ, Unit.CUP, Unit.GAL],
    )
    def test_volume_units_return_volume_dimension(self, unit: Unit) -> None:
        """Test volume units classify as Dimension.VOLUME."""
        assert dimension_of(unit) == Dimension.VOLUME

    @pytest.mark.parametrize("unit", [Unit.EACH, Unit.DOZEN])
    def test_count_units_return_count_dimension(self, unit: Unit) -> None:
        """Test count units classify as Dimension.COUNT."""
        assert dimension_of(unit) == Dimension.COUNT

    @pytest.mark.parametrize("unit", [Unit.BAG, Unit.PINCH, Unit.BUNCH])
    def test_packaging_and_other_units_return_none(self, unit: Unit) -> None:
        """Test non-convertible units (packaging, "other") return None."""
        assert dimension_of(unit) is None


class TestDimensionOfNewUnits:
    """Tests for dimension_of classification of pint/quart/stick (issue #69).

    ``Unit.PINT``/``Unit.QUART``/``Unit.STICK`` don't exist on the current
    ``Unit`` enum yet, so referencing them raises ``AttributeError`` --
    written as plain (non-parametrized) test bodies so a missing attribute
    only fails its own test, not collection of this whole module.
    """

    def test_pint_is_volume(self) -> None:
        """Test Unit.PINT classifies as Dimension.VOLUME."""
        assert dimension_of(Unit.PINT) == Dimension.VOLUME

    def test_quart_is_volume(self) -> None:
        """Test Unit.QUART classifies as Dimension.VOLUME."""
        assert dimension_of(Unit.QUART) == Dimension.VOLUME

    def test_stick_is_dimensionless(self) -> None:
        """Test Unit.STICK (packaging) classifies as None (no fixed dimension)."""
        assert dimension_of(Unit.STICK) is None


# ------------------------------------------------------------------
# Tests: convert
# ------------------------------------------------------------------


class TestConvert:
    """Tests for convert."""

    def test_identity_same_unit(self) -> None:
        """Test converting a unit to itself returns the same quantity."""
        assert convert(5.0, Unit.LB, Unit.LB) == 5.0

    def test_g_to_lb(self) -> None:
        """Test grams to pounds mass conversion."""
        assert convert(453.59237, Unit.G, Unit.LB) == pytest.approx(1.0)

    def test_lb_to_g(self) -> None:
        """Test pounds to grams mass conversion."""
        assert convert(1.0, Unit.LB, Unit.G) == pytest.approx(453.59237)

    def test_oz_to_lb(self) -> None:
        """Test ounces to pounds mass conversion (16 oz == 1 lb)."""
        assert convert(16.0, Unit.OZ, Unit.LB) == pytest.approx(1.0)

    def test_kg_to_g(self) -> None:
        """Test kilograms to grams mass conversion."""
        assert convert(2.0, Unit.KG, Unit.G) == pytest.approx(2000.0)

    def test_g_to_kg(self) -> None:
        """Test grams to kilograms mass conversion."""
        assert convert(2000.0, Unit.G, Unit.KG) == pytest.approx(2.0)

    def test_cup_to_gal(self) -> None:
        """Test cups to gallons volume conversion (16 cups == 1 gal)."""
        assert convert(16.0, Unit.CUP, Unit.GAL) == pytest.approx(1.0)

    def test_gal_to_cup(self) -> None:
        """Test gallons to cups volume conversion."""
        assert convert(1.0, Unit.GAL, Unit.CUP) == pytest.approx(16.0)

    def test_tsp_to_tbsp(self) -> None:
        """Test teaspoons to tablespoons volume conversion (3 tsp == 1 tbsp)."""
        assert convert(3.0, Unit.TSP, Unit.TBSP) == pytest.approx(1.0)

    def test_tbsp_to_tsp(self) -> None:
        """Test tablespoons to teaspoons volume conversion."""
        assert convert(1.0, Unit.TBSP, Unit.TSP) == pytest.approx(3.0)

    def test_l_to_ml(self) -> None:
        """Test liters to milliliters volume conversion."""
        assert convert(1.0, Unit.L, Unit.ML) == pytest.approx(1000.0)

    def test_ml_to_l(self) -> None:
        """Test milliliters to liters volume conversion."""
        assert convert(1000.0, Unit.ML, Unit.L) == pytest.approx(1.0)

    def test_dozen_to_each(self) -> None:
        """Test dozen to each count conversion."""
        assert convert(1.0, Unit.DOZEN, Unit.EACH) == pytest.approx(12.0)

    def test_each_to_dozen(self) -> None:
        """Test each to dozen count conversion."""
        assert convert(12.0, Unit.EACH, Unit.DOZEN) == pytest.approx(1.0)

    def test_cross_dimension_lb_to_cup_returns_none(self) -> None:
        """Test mass-to-volume conversion is rejected."""
        assert convert(1.0, Unit.LB, Unit.CUP) is None

    def test_cross_dimension_each_to_g_returns_none(self) -> None:
        """Test count-to-mass conversion is rejected."""
        assert convert(1.0, Unit.EACH, Unit.G) is None

    def test_non_convertible_bag_to_g_returns_none(self) -> None:
        """Test a non-convertible packaging unit (bag) returns None."""
        assert convert(1.0, Unit.BAG, Unit.G) is None

    def test_non_convertible_g_to_bag_returns_none(self) -> None:
        """Test converting into a non-convertible packaging unit returns None."""
        assert convert(1.0, Unit.G, Unit.BAG) is None


class TestConvertNewUnits:
    """Tests for convert() involving pint/quart (issue #69).

    ``_VOLUME_FACTORS_ML`` lacks entries for ``Unit.PINT``/``Unit.QUART`` on
    the current implementation, so these conversions currently return
    ``None`` instead of the expected converted value.
    """

    def test_pint_to_quart(self) -> None:
        """Test 2 pints convert to approximately 1 quart."""
        assert convert(2.0, Unit.PINT, Unit.QUART) == pytest.approx(1.0)

    def test_quart_to_liter(self) -> None:
        """Test 1 quart converts to approximately 0.946352946 liters."""
        assert convert(1.0, Unit.QUART, Unit.L) == pytest.approx(0.946352946)

    def test_gallon_to_quart(self) -> None:
        """Test 1 gallon converts to exactly 4 quarts."""
        assert convert(1.0, Unit.GAL, Unit.QUART) == pytest.approx(4.0)


# ------------------------------------------------------------------
# Tests: parse_size
# ------------------------------------------------------------------


class TestParseSize:
    """Tests for parse_size."""

    def test_pounds(self) -> None:
        """Test parsing '5 lb'."""
        assert parse_size("5 lb") == (5.0, Unit.LB)

    def test_ounces(self) -> None:
        """Test parsing '16 oz'."""
        assert parse_size("16 oz") == (16.0, Unit.OZ)

    def test_gallon(self) -> None:
        """Test parsing '1 gal'."""
        assert parse_size("1 gal") == (1.0, Unit.GAL)

    def test_fluid_ounces_with_space(self) -> None:
        """Test parsing '32 fl oz' (two-word unit token)."""
        assert parse_size("32 fl oz") == (32.0, Unit.FL_OZ)

    def test_decimal_liters(self) -> None:
        """Test parsing '1.5 l'."""
        assert parse_size("1.5 l") == (1.5, Unit.L)

    def test_bare_number_defaults_to_each(self) -> None:
        """Test a bare number like '12' parses as (12.0, Unit.EACH)."""
        assert parse_size("12") == (12.0, Unit.EACH)

    def test_empty_string_returns_none(self) -> None:
        """Test an empty size string is unparseable."""
        assert parse_size("") is None

    def test_no_leading_number_returns_none(self) -> None:
        """Test a string with no leading number is unparseable."""
        assert parse_size("each") is None

    def test_unrecognized_unit_token_returns_none(self) -> None:
        """Test a strict resolver: unknown unit tokens return None.

        This is the key behavioral difference from ``parse_unit`` in
        ``grocery_butler.models``, which falls back to ``Unit.EACH`` for
        unrecognized tokens. ``parse_size`` must NOT do that fallback --
        garbage like "5 zorbs" must not silently resolve to 5 each.
        """
        assert parse_size("5 zorbs") is None

    def test_leading_whitespace(self) -> None:
        """Test size with leading whitespace is still parsed."""
        assert parse_size("  16 oz") == (16.0, Unit.OZ)


class TestParseSizeNewUnits:
    """Tests for parse_size() recognizing pint/quart tokens (issue #69)."""

    def test_parses_pint(self) -> None:
        """Test '1 pint' parses to (1.0, Unit.PINT)."""
        assert parse_size("1 pint") == (1.0, Unit.PINT)

    def test_parses_quart_abbreviation(self) -> None:
        """Test '2 qt' parses to (2.0, Unit.QUART)."""
        assert parse_size("2 qt") == (2.0, Unit.QUART)


# ------------------------------------------------------------------
# Tests: group_key (issue #80)
# ------------------------------------------------------------------
#
# `units.group_key` is the public helper the issue #80 fix added
# (written RED-first, before the helper existed), mirroring the
# semantics of `Consolidator._ingredient_group_key`, which now delegates
# to it: units with a fixed physical dimension (mass/volume/count) group
# by dimension so compatible units merge; packaging/"other" units with
# no fixed dimension group by their exact unit so incompatible packaging
# (e.g. jar vs. can) stays split. Referenced via `units.group_key(...)`
# (not a top-level `from ... import group_key`) so a missing attribute
# would fail only these tests, not collection of the whole module --
# same pattern as TestDimensionOfNewUnits above.


class TestGroupKey:
    """Tests for the new units.group_key helper (issue #80)."""

    def test_dimensioned_units_group_by_dimension_not_exact_unit(self) -> None:
        """Test cup and gallon (both volume) produce the same group key.

        Dimensioned units must group by physical dimension, not exact
        unit, so compatible units (e.g. cup and gallon) merge into a
        single shopping-list/cart line.
        """
        assert units.group_key("milk", Unit.CUP) == units.group_key("milk", Unit.GAL)

    def test_group_key_lowercases_ingredient_name(self) -> None:
        """Test the ingredient name is lowercased for case-insensitive matching."""
        assert units.group_key("MILK", Unit.CUP) == units.group_key("milk", Unit.CUP)

    def test_dimensioned_group_key_token_is_dimension_prefixed(self) -> None:
        """Test a dimensioned unit's group token starts with 'dim:'."""
        _, token = units.group_key("milk", Unit.CUP)
        assert token == "dim:volume"

    def test_packaging_units_group_by_exact_unit_jar_vs_can(self) -> None:
        """Test jar and can (both dimensionless packaging) produce different keys.

        Packaging/"other" units have no fixed physical size, so
        incompatible packaging (e.g. jar vs. can) must stay split into
        separate line items rather than merging.
        """
        assert units.group_key("pickles", Unit.JAR) != units.group_key(
            "pickles", Unit.CAN
        )

    def test_packaging_group_key_token_is_unit_prefixed(self) -> None:
        """Test a packaging unit's group token starts with 'unit:'."""
        _, token = units.group_key("pickles", Unit.JAR)
        assert token == "unit:jar"
