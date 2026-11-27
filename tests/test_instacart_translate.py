"""Tests for translating a shopping plan into Instacart line items (#147).

Translation loses things: a unit Instacart doesn't know, a rule it can't
enforce. These tests hold that every loss is reported and none is silent.
"""

from __future__ import annotations

import datetime as dt
import json
import random
from typing import Any

import pytest

from grocery_butler.fulfillment.instacart.translate import (
    UNIT_MAP,
    IssueKind,
    TranslationResult,
    translate,
)
from grocery_butler.models import (
    HardConstraintKind,
    IngredientCategory,
    NeedReasonKind,
    PlanAmount,
    PlanHardConstraint,
    PlanLine,
    PlanReason,
    ShoppingPlan,
    Unit,
)

NOW = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.UTC)
PEANUT = PlanHardConstraint(
    kind=HardConstraintKind.ALLERGEN_EXCLUSION, value="peanut", scope="household"
)
VEGAN = PlanHardConstraint(
    kind=HardConstraintKind.DIETARY_RULE, value="vegan", scope="household"
)
EXACT = PlanHardConstraint(
    kind=HardConstraintKind.DO_NOT_SUBSTITUTE, value="", scope="item"
)


def _line(
    item_id: int,
    name: str,
    amounts: list[tuple[float, Unit]] | None = None,
    *,
    prefer: dict[str, Any] | None = None,
    must: list[PlanHardConstraint] | None = None,
) -> PlanLine:
    """Build one plan line."""
    return PlanLine(
        item_id=item_id,
        display_name=name,
        category=IngredientCategory.PRODUCE,
        amounts=[PlanAmount(quantity=q, unit=u) for q, u in (amounts or [])],
        reasons=[
            PlanReason(kind=NeedReasonKind.MANUAL_REQUEST, source_key="t", detail={})
        ],
        constraints=prefer or {},
        need_id=item_id,
        hard_constraints=must or [],
    )


def _plan(*lines: PlanLine, version: int = 7) -> ShoppingPlan:
    """Build a plan version from lines."""
    return ShoppingPlan(
        id=version,
        version=version,
        content_hash=f"hash-{version}",
        meals=[],
        unplanned=[],
        created_by="geoff@test",
        created_at=NOW,
        lines=list(lines),
    )


def _items(result: TranslationResult) -> list[dict[str, Any]]:
    """Return the request's line items as plain JSON."""
    assert result.request is not None
    items: list[dict[str, Any]] = json.loads(json.dumps(result.request.body()))[
        "line_items"
    ]
    return items


def _kinds(result: TranslationResult) -> list[IssueKind]:
    """Return the kinds of the issues, in order."""
    return [issue.kind for issue in result.issues]


class TestExamples:
    """The examples in the issue, one test each."""

    def test_two_pounds_of_carrots(self) -> None:
        """A weight becomes a measurement in a supported unit."""
        result = translate(_plan(_line(1, "Carrots", [(2, Unit.LB)])), link_days=14)

        assert _items(result) == [
            {
                "name": "carrots",
                "display_text": "Carrots",
                "line_item_measurements": [{"quantity": 2.0, "unit": "lb"}],
                "upcs": [],
            }
        ]
        assert result.issues == ()

    def test_three_sweet_potatoes(self) -> None:
        """A count becomes ``each``."""
        result = translate(
            _plan(_line(1, "Sweet Potatoes", [(3, Unit.EACH)])), link_days=14
        )

        assert _items(result)[0]["line_item_measurements"] == [
            {"quantity": 3.0, "unit": "each"}
        ]

    def test_no_peanut_products_can_not_be_expressed(self) -> None:
        """An allergen exclusion is reported, blocks, and is never a filter."""
        result = translate(_plan(_line(1, "Granola Bars", must=[PEANUT])), link_days=14)

        assert _kinds(result) == [IssueKind.HARD_CONSTRAINT_UNREPRESENTABLE]
        assert result.issues[0].item_id == 1
        assert result.issues[0].display_name == "Granola Bars"
        assert "peanut" in result.issues[0].detail
        assert result.needs_acknowledgement is True
        assert "filters" not in _items(result)[0]
        assert "peanut" not in json.dumps(_items(result))

    def test_organic_milk_with_a_verified_upc(self) -> None:
        """The UPC is sent, the organic filter would be ignored, and we say so."""
        plan = _plan(_line(4, "Milk", prefer={"organic": True}))

        result = translate(plan, link_days=14, verified_upcs={4: "012345678905"})

        assert _items(result)[0]["upcs"] == ["012345678905"]
        assert "filters" not in _items(result)[0]
        assert _kinds(result) == [IssueKind.FILTER_DROPPED_BY_UPC]
        assert "organic" in result.issues[0].detail
        assert result.needs_acknowledgement is False


class TestMeasurements:
    """Amounts are sent in Instacart's units or not at all."""

    @pytest.mark.parametrize(
        ("unit", "expected"),
        [
            (Unit.TSP, "teaspoon"),
            (Unit.TBSP, "tablespoon"),
            (Unit.CUP, "cup"),
            (Unit.FL_OZ, "fl oz"),
            (Unit.ML, "ml"),
            (Unit.L, "liter"),
            (Unit.GAL, "gallon"),
            (Unit.PINT, "pint"),
            (Unit.QUART, "quart"),
            (Unit.OZ, "oz"),
            (Unit.LB, "lb"),
            (Unit.G, "gram"),
            (Unit.KG, "kilogram"),
            (Unit.EACH, "each"),
            (Unit.BUNCH, "bunch"),
            (Unit.HEAD, "head"),
            (Unit.CAN, "can"),
            (Unit.PACKAGE, "package"),
        ],
    )
    def test_supported_units(self, unit: Unit, expected: str) -> None:
        """Each mapped unit is sent under Instacart's spelling."""
        result = translate(_plan(_line(1, "Thing", [(1.5, unit)])), link_days=14)

        assert _items(result)[0]["line_item_measurements"] == [
            {"quantity": 1.5, "unit": expected}
        ]
        assert result.issues == ()

    def test_a_dozen_is_twelve_each(self) -> None:
        """A dozen has an exact conversion, so it is converted."""
        result = translate(_plan(_line(1, "Eggs", [(2, Unit.DOZEN)])), link_days=14)

        assert _items(result)[0]["line_item_measurements"] == [
            {"quantity": 24.0, "unit": "each"}
        ]

    @pytest.mark.parametrize("unit", [unit for unit in Unit if unit not in UNIT_MAP])
    def test_an_unsupported_unit_sends_no_measurement_and_says_so(
        self, unit: Unit
    ) -> None:
        """A unit Instacart doesn't list is never guessed at."""
        result = translate(_plan(_line(1, "Garlic", [(3, unit)])), link_days=14)

        assert _items(result)[0]["line_item_measurements"] == []
        assert _kinds(result) == [IssueKind.UNSUPPORTED_UNIT]
        assert unit.value in result.issues[0].detail
        assert "3" in result.issues[0].detail

    def test_every_unit_is_either_mapped_or_reported(self) -> None:
        """No unit falls through: the map and the unsupported set cover Unit."""
        assert len(set(Unit) - set(UNIT_MAP)) >= 5
        assert set(UNIT_MAP) <= set(Unit)

    def test_amounts_in_two_units_are_not_combined(self) -> None:
        """Instacart reads several measurements as alternatives, so send none."""
        line = _line(1, "Butter", [(1, Unit.CUP), (200, Unit.G)])

        result = translate(_plan(line), link_days=14)

        assert _items(result)[0]["line_item_measurements"] == []
        assert _kinds(result) == [IssueKind.AMBIGUOUS_MAPPING]
        assert "1 cup" in result.issues[0].detail
        assert "200 g" in result.issues[0].detail

    def test_no_amount_means_the_usual(self) -> None:
        """A line with no amount is sent by name with nothing to report."""
        result = translate(_plan(_line(1, "Oat Milk")), link_days=14)

        assert _items(result)[0]["line_item_measurements"] == []
        assert result.issues == ()

    def test_a_non_positive_amount_is_reported_not_sent(self) -> None:
        """Zero is a documented 400, so it is never sent."""
        result = translate(_plan(_line(1, "Salt", [(0, Unit.G)])), link_days=14)

        assert _items(result)[0]["line_item_measurements"] == []
        assert _kinds(result) == [IssueKind.AMBIGUOUS_MAPPING]


class TestPreferences:
    """Soft preferences become filters or visible text."""

    def test_a_preferred_brand_is_a_brand_filter(self) -> None:
        """The brand is sent as the household spells it."""
        line = _line(1, "Oat Milk", prefer={"preferred_brand": "Oatly"})

        result = translate(_plan(line), link_days=14)

        assert _items(result)[0]["filters"] == {
            "brand_filters": ["Oatly"],
            "health_filters": [],
        }
        assert result.issues == ()

    def test_organic_is_a_health_filter(self) -> None:
        """Organic is in Instacart's closed set."""
        line = _line(1, "Milk", prefer={"organic": True})

        result = translate(_plan(line), link_days=14)

        assert _items(result)[0]["filters"]["health_filters"] == ["ORGANIC"]

    def test_a_preferred_size_is_shown_to_the_shopper(self) -> None:
        """Instacart has no size field, so the size goes in the visible text."""
        line = _line(1, "Oat Milk", prefer={"preferred_size": "64 oz"})

        result = translate(_plan(line), link_days=14)

        assert _items(result)[0]["display_text"] == "Oat Milk (64 oz)"
        assert _items(result)[0]["name"] == "oat milk"
        assert "filters" not in _items(result)[0]

    def test_blank_preferences_send_nothing(self) -> None:
        """Empty strings and false flags are not preferences."""
        line = _line(
            1,
            "Oat Milk",
            prefer={"preferred_brand": " ", "preferred_size": "", "organic": False},
        )

        result = translate(_plan(line), link_days=14)

        assert "filters" not in _items(result)[0]
        assert _items(result)[0]["display_text"] == "Oat Milk"

    def test_a_preference_instacart_cannot_filter_is_left_out_and_reported(
        self,
    ) -> None:
        """Store brand and brands to avoid are not sent, and it says so once."""
        line = _line(
            1,
            "Oat Milk",
            prefer={"generic": True, "avoid_brands": ["Silk", "Planet Oat"]},
        )

        result = translate(_plan(line), link_days=14)

        sent = json.dumps(_items(result))
        assert "filters" not in _items(result)[0]
        assert "Silk" not in sent
        assert "store brand" not in sent
        assert _kinds(result) == [IssueKind.PREFERENCE_NOT_APPLIED]
        assert result.issues[0].detail == (
            "prefer the store brand; avoid Silk, Planet Oat"
        )
        assert result.needs_acknowledgement is False

    def test_organic_is_still_filtered_beside_an_unfilterable_preference(
        self,
    ) -> None:
        """What Instacart can apply is applied; only the rest is reported."""
        line = _line(1, "Oat Milk", prefer={"organic": True, "generic": True})

        result = translate(_plan(line), link_days=14)

        assert _items(result)[0]["filters"]["health_filters"] == ["ORGANIC"]
        assert _kinds(result) == [IssueKind.PREFERENCE_NOT_APPLIED]

    def test_a_code_on_the_line_is_sent_and_the_dropped_filter_reported(
        self,
    ) -> None:
        """The plan line carries the verified code; no side table is needed."""
        line = _line(
            4, "Milk", prefer={"verified_upc": "012345678905", "organic": True}
        )

        result = translate(_plan(line), link_days=14)

        assert _items(result)[0]["upcs"] == ["012345678905"]
        assert "filters" not in _items(result)[0]
        assert _kinds(result) == [IssueKind.FILTER_DROPPED_BY_UPC]

    def test_an_unknown_preference_is_reported(self) -> None:
        """A preference this translation doesn't understand is never dropped."""
        line = _line(1, "Oat Milk", prefer={"shelf_stable": True})

        result = translate(_plan(line), link_days=14)

        assert _kinds(result) == [IssueKind.AMBIGUOUS_MAPPING]
        assert "shelf_stable" in result.issues[0].detail


class TestHardConstraints:
    """A hard constraint is never weakened into a filter."""

    @pytest.mark.parametrize("constraint", [PEANUT, VEGAN, EXACT])
    def test_every_kind_is_unrepresentable(
        self, constraint: PlanHardConstraint
    ) -> None:
        """Even a rule that looks like a health filter is reported, not sent."""
        result = translate(_plan(_line(1, "Burgers", must=[constraint])), link_days=14)

        assert _kinds(result) == [IssueKind.HARD_CONSTRAINT_UNREPRESENTABLE]
        assert constraint.kind.value in result.issues[0].detail
        assert result.needs_acknowledgement is True
        assert "filters" not in _items(result)[0]
        assert "VEGAN" not in json.dumps(_items(result))

    def test_each_constraint_on_each_line_is_its_own_issue(self) -> None:
        """Two rules on one line and one on another are three issues."""
        plan = _plan(
            _line(1, "Burgers", must=[PEANUT, VEGAN]), _line(2, "Buns", must=[PEANUT])
        )

        result = translate(plan, link_days=14)

        assert [(i.item_id, i.kind) for i in result.issues] == [
            (1, IssueKind.HARD_CONSTRAINT_UNREPRESENTABLE),
            (1, IssueKind.HARD_CONSTRAINT_UNREPRESENTABLE),
            (2, IssueKind.HARD_CONSTRAINT_UNREPRESENTABLE),
        ]

    def test_a_plan_without_hard_constraints_needs_no_acknowledgement(self) -> None:
        """Other issues inform; only a hard constraint blocks."""
        result = translate(_plan(_line(1, "Garlic", [(3, Unit.CLOVE)])), link_days=14)

        assert result.issues != ()
        assert result.needs_acknowledgement is False


class TestUpcs:
    """A UPC is sent only when it is a verified household fact."""

    @pytest.mark.parametrize("upc", ["012345678905", "00012345678905"])
    def test_a_valid_upc_is_sent(self, upc: str) -> None:
        """12 and 14 digit codes are accepted."""
        result = translate(
            _plan(_line(4, "Milk")), link_days=14, verified_upcs={4: upc}
        )

        assert _items(result)[0]["upcs"] == [upc]
        assert result.issues == ()

    @pytest.mark.parametrize("upc", ["", "12345", "0123456789055", "01234567890X"])
    def test_an_invalid_upc_is_reported_and_not_sent(self, upc: str) -> None:
        """A malformed code is dropped, and the line falls back to its name."""
        plan = _plan(_line(4, "Milk", prefer={"organic": True}))

        result = translate(plan, link_days=14, verified_upcs={4: upc})

        assert _items(result)[0]["upcs"] == []
        assert _items(result)[0]["filters"]["health_filters"] == ["ORGANIC"]
        assert _kinds(result) == [IssueKind.INVALID_UPC]

    def test_a_upc_used_twice_is_sent_once(self) -> None:
        """Instacart rejects duplicates across lines, so the second is reported."""
        plan = _plan(_line(4, "Milk"), _line(5, "Whole Milk"))
        upcs = {4: "012345678905", 5: "012345678905"}

        result = translate(plan, link_days=14, verified_upcs=upcs)

        assert [item["upcs"] for item in _items(result)] == [["012345678905"], []]
        assert [(i.item_id, i.kind) for i in result.issues] == [
            (5, IssueKind.INVALID_UPC)
        ]

    def test_a_upc_for_an_item_not_on_the_plan_is_ignored(self) -> None:
        """Only the plan's own lines are sent."""
        result = translate(
            _plan(_line(4, "Milk")), link_days=14, verified_upcs={99: "012345678905"}
        )

        assert _items(result)[0]["upcs"] == []
        assert result.issues == ()

    def test_brand_and_organic_are_both_reported_when_a_upc_drops_them(self) -> None:
        """Each dropped filter is named."""
        line = _line(4, "Milk", prefer={"organic": True, "preferred_brand": "Clover"})

        result = translate(_plan(line), link_days=14, verified_upcs={4: "012345678905"})

        assert _kinds(result) == [IssueKind.FILTER_DROPPED_BY_UPC]
        assert "Clover" in result.issues[0].detail
        assert "organic" in result.issues[0].detail


class TestRequest:
    """The whole request."""

    def test_title_expiry_and_order(self) -> None:
        """Lines keep the plan's order; the expiry is always set."""
        plan = _plan(_line(2, "Bananas"), _line(1, "Apples"), version=9)

        result = translate(plan, link_days=5)

        assert result.request is not None
        body = result.request.body()
        assert body["title"] == "Grocery Butler shopping plan 9"
        assert body["expires_in"] == 5
        assert body["link_type"] == "shopping_list"
        assert [item["name"] for item in _items(result)] == ["bananas", "apples"]

    def test_an_empty_plan_has_no_request(self) -> None:
        """Nothing to send is not an error; there is simply no request."""
        result = translate(_plan(), link_days=14)

        assert result.request is None
        assert result.issues == ()
        assert result.needs_acknowledgement is False

    def test_the_same_plan_gives_a_byte_identical_body(self) -> None:
        """Translation is deterministic for a plan version."""
        plan = _plan(
            _line(1, "Carrots", [(2, Unit.LB)], prefer={"preferred_brand": "Bunny"}),
            _line(2, "Garlic", [(3, Unit.CLOVE)], must=[PEANUT]),
        )

        first = translate(plan, link_days=14, verified_upcs={1: "012345678905"})
        again = translate(plan, link_days=14, verified_upcs={1: "012345678905"})

        assert first.request is not None
        assert again.request is not None
        assert (
            json.dumps(first.request.body(), sort_keys=True).encode()
            == json.dumps(again.request.body(), sort_keys=True).encode()
        )
        assert first.issues == again.issues

    def test_issues_are_plain_data(self) -> None:
        """An issue can be shown to a person or stored as JSON."""
        result = translate(_plan(_line(1, "Garlic", [(3, Unit.CLOVE)])), link_days=14)

        assert result.issues[0].as_dict() == {
            "kind": "unsupported_unit",
            "item_id": 1,
            "display_name": "Garlic",
            "detail": result.issues[0].detail,
        }


class TestNothingIsDroppedSilently:
    """Across many generated plans, every part of every line is accounted for."""

    @staticmethod
    def _random_line(rng: random.Random, item_id: int) -> PlanLine:
        """Build a line with random amounts, preferences and constraints."""
        amounts = [
            (rng.choice([0.5, 1, 2, 12]), rng.choice(list(Unit)))
            for _ in range(rng.randint(0, 2))
        ]
        prefer: dict[str, Any] = {}
        if rng.random() < 0.4:
            prefer["preferred_brand"] = "Brand"
        if rng.random() < 0.3:
            prefer["preferred_size"] = "large"
        if rng.random() < 0.3:
            prefer["organic"] = True
        must = [c for c in (PEANUT, VEGAN, EXACT) if rng.random() < 0.25]
        return _line(item_id, f"Item {item_id}", amounts, prefer=prefer, must=must)

    @pytest.mark.parametrize("seed", range(40))
    def test_every_line_part_is_sent_or_reported(self, seed: int) -> None:
        """Each line is a line item; each amount, preference and rule is
        either in the request or in an issue."""
        rng = random.Random(seed)
        lines = [self._random_line(rng, i) for i in range(1, rng.randint(2, 9))]
        upcs = {
            line.item_id: "012345678905" for line in lines[:1] if rng.random() < 0.5
        }

        result = translate(_plan(*lines), link_days=14, verified_upcs=upcs)
        items = _items(result)

        assert len(items) == len(lines)
        for line, item in zip(lines, items, strict=True):
            mine = [issue for issue in result.issues if issue.item_id == line.item_id]
            kinds = [issue.kind for issue in mine]
            assert item["name"] == line.display_name.lower()
            assert kinds.count(IssueKind.HARD_CONSTRAINT_UNREPRESENTABLE) == len(
                line.hard_constraints
            )
            if line.amounts and not item["line_item_measurements"]:
                assert {IssueKind.UNSUPPORTED_UNIT, IssueKind.AMBIGUOUS_MAPPING} & set(
                    kinds
                )
            wants_filter = bool(
                line.constraints.get("preferred_brand")
                or line.constraints.get("organic")
            )
            if wants_filter and "filters" not in item:
                assert IssueKind.FILTER_DROPPED_BY_UPC in kinds
            if line.constraints.get("preferred_size"):
                assert "large" in item["display_text"]
