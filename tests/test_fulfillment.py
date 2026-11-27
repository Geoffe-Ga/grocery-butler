"""Tests for the fulfillment adapter interface and the list export (#144).

Contract tests run against every registered adapter. Golden files pin each
export format. An import-boundary test keeps concrete adapters out of the
core domain modules (ADR 0001).
"""

from __future__ import annotations

import ast
import csv
import datetime as dt
import io
import json
from pathlib import Path
from typing import Any

import pytest

from grocery_butler.fulfillment.base import (
    AdapterDescription,
    Authorization,
    Capability,
    FulfillmentAdapter,
    HandoffResult,
    HandoffStatus,
    UnenforcedConstraint,
    UnsupportedCapabilityError,
    unenforced_constraints,
)
from grocery_butler.fulfillment.export import EXPORT_FORMATS, ExportAdapter
from grocery_butler.fulfillment.instacart.adapter import InstacartAdapter
from grocery_butler.fulfillment.instacart.transport import FakeTransport
from grocery_butler.fulfillment.registry import (
    AdapterNotFoundError,
    AdapterRegistry,
    default_registry,
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

GOLDEN = Path(__file__).parent / "golden"
PACKAGE = Path(__file__).resolve().parent.parent / "grocery_butler"

# Every adapter that exists, enabled in this deployment or not, must honour
# the contract. The Instacart adapter runs on a fake transport.
CONTRACT_ADAPTERS: list[FulfillmentAdapter] = [
    ExportAdapter(),
    InstacartAdapter(FakeTransport.ok("https://example.invalid/l/contract")),
]
CONTRACT_ADAPTER_IDS = [adapter.describe().name for adapter in CONTRACT_ADAPTERS]

# Domain modules that must never import a concrete adapter.
CORE_MODULES = [
    "approvals",
    "check_reminders",
    "consolidator",
    "constraints",
    "household_items",
    "inventory_ledger",
    "members",
    "models",
    "observation_batches",
    "pantry_manager",
    "pending_actions",
    "recipe_store",
    "reconciliation",
    "replenishment",
    "shopping_plans",
]


def _reason(kind: NeedReasonKind, recipe: str | None = None) -> PlanReason:
    """Build a plan reason, a recipe's when a recipe name is given."""
    detail = {"recipe": recipe} if recipe else {}
    key = f"recipe:{recipe}" if recipe else "inventory"
    return PlanReason(kind=kind, source_key=key, detail=detail)


PEANUT = PlanHardConstraint(
    kind=HardConstraintKind.ALLERGEN_EXCLUSION, value="peanut", scope="household"
)
VEGETARIAN = PlanHardConstraint(
    kind=HardConstraintKind.DIETARY_RULE, value="vegetarian", scope="household"
)
NO_SUBSTITUTES = PlanHardConstraint(
    kind=HardConstraintKind.DO_NOT_SUBSTITUTE, value="", scope="item"
)


def _line(
    item_id: int,
    name: str,
    category: IngredientCategory | None,
    amounts: list[tuple[float, Unit]],
    reasons: list[PlanReason],
    must: list[PlanHardConstraint] | None = None,
    **constraints: Any,
) -> PlanLine:
    """Build a plan line."""
    return PlanLine(
        item_id=item_id,
        display_name=name,
        category=category,
        amounts=[PlanAmount(quantity=q, unit=u) for q, u in amounts],
        reasons=reasons,
        constraints=dict(constraints),
        need_id=None,
        hard_constraints=must or [],
    )


@pytest.fixture()
def plan() -> ShoppingPlan:
    """Return a plan covering every shape of line the export has to render."""
    recipe = NeedReasonKind.RECIPE_DEMAND
    return ShoppingPlan(
        id=1,
        version=3,
        content_hash="golden-hash",
        meals=[],
        unplanned=[],
        created_by="geoff@rubotpaul",
        created_at=dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.UTC),
        lines=[
            _line(
                1,
                "Buttermilk",
                IngredientCategory.DAIRY,
                [(4.0, Unit.CUP)],
                [
                    _reason(NeedReasonKind.LOW_STOCK),
                    _reason(recipe, "Biscuits"),
                    _reason(recipe, "Pancakes"),
                ],
                [PEANUT, VEGETARIAN, NO_SUBSTITUTES],
                preferred_brand="Clover",
                preferred_size="1 qt",
            ),
            _line(
                2,
                "Oat Milk",
                IngredientCategory.DAIRY,
                [],
                [_reason(NeedReasonKind.OUT)],
                [PEANUT, VEGETARIAN],
                organic=True,
                generic=True,
                avoid_brands=["Silk", "Planet Oat"],
            ),
            _line(
                3,
                "Yogurt",
                IngredientCategory.DAIRY,
                [(1.0, Unit.CUP), (2.0, Unit.JAR)],
                [_reason(recipe, "Curry"), _reason(recipe, "Parfait")],
            ),
            _line(
                4,
                "Rice",
                IngredientCategory.PANTRY_DRY,
                [(1.5, Unit.LB)],
                [_reason(NeedReasonKind.MANUAL_REQUEST), _reason(recipe, "Curry")],
            ),
            _line(
                5,
                "Birthday Candles",
                None,
                [],
                [_reason(NeedReasonKind.MANUAL_REQUEST)],
            ),
        ],
    )


class TestExportGoldenFiles:
    """Each export format is pinned byte for byte."""

    @pytest.mark.parametrize(
        ("fmt", "golden"),
        [
            ("text", "plan_export.txt"),
            ("csv", "plan_export.csv"),
            ("json", "plan_export.json"),
        ],
    )
    def test_format_matches_its_golden_file(
        self, plan: ShoppingPlan, fmt: str, golden: str
    ) -> None:
        """A change to an export's shape must be a deliberate golden update."""
        result = ExportAdapter().create_handoff(plan)

        assert result.artifacts[fmt] == (GOLDEN / golden).read_text()

    def test_every_declared_format_is_produced(self, plan: ShoppingPlan) -> None:
        """The adapter produces exactly the formats it names."""
        result = ExportAdapter().create_handoff(plan)

        assert sorted(result.artifacts) == sorted(EXPORT_FORMATS)
        assert sorted(EXPORT_FORMATS) == ["csv", "json", "text"]

    def test_json_export_is_valid_json_and_csv_parses(self, plan: ShoppingPlan) -> None:
        """Machine formats are well formed."""
        artifacts = ExportAdapter().create_handoff(plan).artifacts

        rows = list(csv.DictReader(io.StringIO(artifacts["csv"])))
        assert json.loads(artifacts["json"])["plan_version"] == 3
        assert len(rows) == 6
        assert rows[1]["item"] == "Oat Milk"
        assert rows[1]["quantity"] == ""

    def test_every_soft_preference_is_shown_in_every_format(
        self, plan: ShoppingPlan
    ) -> None:
        """Organic, store brand and brands to avoid reach the list (#215)."""
        artifacts = ExportAdapter().create_handoff(plan).artifacts

        rows = list(csv.DictReader(io.StringIO(artifacts["csv"])))
        oat_milk = json.loads(artifacts["json"])["categories"][0]["lines"][1]
        assert (
            "Oat Milk: the usual (out)"
            " [prefer organic, the store brand; avoid Silk, Planet Oat]"
        ) in artifacts["text"]
        assert (oat_milk["organic"], oat_milk["generic"]) == (True, True)
        assert oat_milk["avoid_brands"] == ["Silk", "Planet Oat"]
        assert rows[1]["other_preferences"] == (
            "organic; prefer the store brand; avoid Silk, Planet Oat"
        )
        assert rows[0]["other_preferences"] == ""

    def test_empty_plan_exports_cleanly(self, plan: ShoppingPlan) -> None:
        """Nothing to buy is still a valid export."""
        empty = plan.model_copy(update={"lines": []})

        artifacts = ExportAdapter().create_handoff(empty).artifacts

        assert artifacts["text"] == "Shopping plan v3\n\nNothing to buy.\n"
        assert artifacts["csv"].splitlines() == [
            "category,item,quantity,unit,reasons,preferred_brand,preferred_size,"
            "other_preferences,must"
        ]
        assert json.loads(artifacts["json"])["categories"] == []

    @pytest.mark.parametrize("name", ["=cmd()", "+1+1", "-2+3", "@SUM(A1)"])
    def test_csv_cells_cannot_become_spreadsheet_formulas(
        self, plan: ShoppingPlan, name: str
    ) -> None:
        """An item name can't run as a formula when the CSV is opened."""
        hostile = plan.model_copy(
            update={"lines": [_line(9, name, None, [], [_reason(NeedReasonKind.OUT)])]}
        )

        text = ExportAdapter().create_handoff(hostile).artifacts["csv"]
        row = next(csv.DictReader(io.StringIO(text)))

        assert row["item"] == f"'{name}"

    def test_fractional_amounts_are_not_padded(self, plan: ShoppingPlan) -> None:
        """4.0 prints as 4, 1.5 stays 1.5."""
        text = ExportAdapter().create_handoff(plan).artifacts["text"]

        assert "4 cup" in text
        assert "4.0 cup" not in text
        assert "1.5 lb" in text


class TestExportHandoff:
    """The export's result says what it is, and never claims an order."""

    def test_result_identifies_the_plan_and_tells_the_person_what_to_do(
        self, plan: ShoppingPlan
    ) -> None:
        """A handoff carries the plan version and hash it was made from."""
        result = ExportAdapter().create_handoff(plan)

        assert result.adapter == "export"
        assert result.status is HandoffStatus.CREATED
        assert (result.plan_version, result.plan_content_hash) == (3, "golden-hash")
        assert result.external_url is None
        assert result.expires_at is None
        assert "any store" in result.human_action

    def test_export_declares_only_what_it_does(self) -> None:
        """One capability, no authorization, a person needed."""
        description = ExportAdapter().describe()

        assert description == AdapterDescription(
            name="export",
            display_name="Shopping list export",
            capabilities=frozenset({Capability.EXPORT_LIST}),
            authorization=Authorization.NONE,
            data_freshness=description.data_freshness,
            human_required=True,
            enforces=frozenset(),
        )
        assert "plan" in description.data_freshness


class TestHardConstraintsInTheExport:
    """A list can't enforce anything, so it shows and reports everything."""

    def test_every_hard_constraint_is_reported_as_unenforced(
        self, plan: ShoppingPlan
    ) -> None:
        """Five constraints on two lines, five reports, none dropped."""
        result = ExportAdapter().create_handoff(plan)

        assert result.unenforced_constraints == (
            UnenforcedConstraint(1, "Buttermilk", PEANUT.kind, "peanut"),
            UnenforcedConstraint(1, "Buttermilk", VEGETARIAN.kind, "vegetarian"),
            UnenforcedConstraint(1, "Buttermilk", NO_SUBSTITUTES.kind, ""),
            UnenforcedConstraint(2, "Oat Milk", PEANUT.kind, "peanut"),
            UnenforcedConstraint(2, "Oat Milk", VEGETARIAN.kind, "vegetarian"),
        )
        assert "marked MUST" in result.human_action

    def test_a_plan_without_hard_constraints_reports_none(
        self, plan: ShoppingPlan
    ) -> None:
        """No constraints, no warning."""
        plain = plan.model_copy(update={"lines": plan.lines[2:]})

        result = ExportAdapter().create_handoff(plain)

        assert result.unenforced_constraints == ()
        assert "MUST" not in result.human_action
        assert "MUST" not in result.artifacts["text"]

    def test_hard_constraints_are_never_written_as_preferences(
        self, plan: ShoppingPlan
    ) -> None:
        """In every format a constraint sits under must, never under prefer."""
        artifacts = ExportAdapter().create_handoff(plan).artifacts
        document = json.loads(artifacts["json"])
        buttermilk = document["categories"][0]["lines"][0]
        row = next(csv.DictReader(io.StringIO(artifacts["csv"])))
        text_line = artifacts["text"].splitlines()[3]

        assert buttermilk["must"] == ["no peanut", "vegetarian", "no substitutes"]
        assert "peanut" not in json.dumps(
            {k: v for k, v in buttermilk.items() if k != "must"}
        )
        assert row["must"] == "no peanut; vegetarian; no substitutes"
        assert "peanut" not in row["preferred_brand"] + row["preferred_size"]
        prefer, must = text_line.split("{MUST:")
        assert "peanut" not in prefer
        assert "no peanut" in must

    def test_the_export_enforces_nothing_by_itself(self) -> None:
        """It must not claim a guarantee a piece of paper can't give."""
        assert ExportAdapter().describe().enforces == frozenset()


class TestAdapterContract:
    """What every registered adapter must honour."""

    @pytest.mark.parametrize("adapter", CONTRACT_ADAPTERS, ids=CONTRACT_ADAPTER_IDS)
    def test_adapter_enforces_or_reports_every_hard_constraint(
        self, adapter: FulfillmentAdapter, plan: ShoppingPlan
    ) -> None:
        """ADR 0012: a hard constraint is never silently dropped."""
        description = adapter.describe()
        result = adapter.create_handoff(plan)
        reported = {
            (entry.item_id, entry.kind, entry.value)
            for entry in result.unenforced_constraints
        }

        for line in plan.lines:
            for constraint in line.hard_constraints:
                enforced = constraint.kind in description.enforces
                was_reported = (
                    line.item_id,
                    constraint.kind,
                    constraint.value,
                ) in reported
                assert enforced or was_reported, (line.display_name, constraint)
        assert result.unenforced_constraints == unenforced_constraints(
            description, plan
        )

    @pytest.mark.parametrize("adapter", CONTRACT_ADAPTERS, ids=CONTRACT_ADAPTER_IDS)
    def test_a_plan_leaves_the_household_only_after_review(
        self, adapter: FulfillmentAdapter, plan: ShoppingPlan
    ) -> None:
        """ADR 0012: no outside handoff with an unreviewed hard constraint.

        An adapter that creates something outside the household (it returns
        a URL) must refuse a plan with unenforced hard constraints until a
        person has acknowledged them.
        """
        unreviewed = adapter.create_handoff(plan)
        reviewed = adapter.create_handoff(plan, constraints_acknowledged=True)

        assert unreviewed.unenforced_constraints
        assert unreviewed.external_url is None
        assert reviewed.status is HandoffStatus.CREATED
        if reviewed.external_url is not None:
            assert unreviewed.status is HandoffStatus.FAILED

    def test_an_adapter_that_enforces_a_kind_is_not_asked_to_report_it(
        self, plan: ShoppingPlan
    ) -> None:
        """The helper reports exactly what the adapter doesn't enforce."""
        description = AdapterDescription(
            name="strict",
            display_name="Strict",
            capabilities=frozenset({Capability.EXPORT_LIST}),
            authorization=Authorization.NONE,
            data_freshness="n/a",
            human_required=True,
            enforces=frozenset({HardConstraintKind.ALLERGEN_EXCLUSION}),
        )

        kinds = [entry.kind for entry in unenforced_constraints(description, plan)]

        assert HardConstraintKind.ALLERGEN_EXCLUSION not in kinds
        assert kinds.count(HardConstraintKind.DIETARY_RULE) == 2
        assert kinds.count(HardConstraintKind.DO_NOT_SUBSTITUTE) == 1

    @pytest.mark.parametrize("adapter", CONTRACT_ADAPTERS, ids=CONTRACT_ADAPTER_IDS)
    def test_adapter_honours_the_contract(
        self, adapter: FulfillmentAdapter, plan: ShoppingPlan
    ) -> None:
        """Declared honestly, human in the loop, and a handoff is not an order."""
        description = adapter.describe()
        result = adapter.create_handoff(plan)

        assert description.name
        assert description.name == description.name.lower()
        assert description.capabilities
        assert description.human_required is True
        assert description.data_freshness
        assert isinstance(result, HandoffResult)
        assert result.adapter == description.name
        assert result.status in {HandoffStatus.CREATED, HandoffStatus.FAILED}
        assert result.plan_version == plan.version
        assert result.plan_content_hash == plan.content_hash
        assert result.human_action

    @pytest.mark.parametrize("adapter", CONTRACT_ADAPTERS, ids=CONTRACT_ADAPTER_IDS)
    def test_adapter_refuses_capabilities_it_did_not_declare(
        self, adapter: FulfillmentAdapter
    ) -> None:
        """require() is the guard callers use before relying on a capability."""
        declared = adapter.describe().capabilities

        for capability in Capability:
            if capability in declared:
                adapter.require(capability)
            else:
                with pytest.raises(UnsupportedCapabilityError, match=capability.value):
                    adapter.require(capability)

    def test_no_status_or_capability_can_mean_a_purchase(self) -> None:
        """The vocabulary itself has no word for buying."""
        words = {status.value for status in HandoffStatus} | {
            capability.value for capability in Capability
        }

        assert not any(
            term in word
            for word in words
            for term in ("purchase", "checkout", "pay", "submit", "ordered")
        )

    def test_no_authorization_method_stores_retailer_credentials(self) -> None:
        """ADR 0001: no adapter may hold a retailer password."""
        assert {method.value for method in Authorization} == {
            "none",
            "provider_program",
            "user_session",
        }


class TestRegistry:
    """Adapters are found by name."""

    def test_default_registry_offers_the_export(self) -> None:
        """Every deployment can export a plan for any store."""
        registry = default_registry()

        assert registry.names() == ["export"]
        assert isinstance(registry.get("export"), ExportAdapter)

    def test_unknown_adapter_lists_what_exists(self) -> None:
        """The error tells the caller what it could have asked for."""
        with pytest.raises(AdapterNotFoundError, match="Available: export"):
            default_registry().get("safeway")

    def test_empty_registry_says_none(self) -> None:
        """An empty registry still gives a clear error."""
        with pytest.raises(AdapterNotFoundError, match="Available: none"):
            AdapterRegistry().get("export")

    def test_a_name_can_be_registered_once(self) -> None:
        """Two adapters can't share a name."""
        registry = default_registry()

        with pytest.raises(ValueError, match="already registered"):
            registry.register(ExportAdapter())


class TestImportBoundary:
    """Core modules never import a concrete adapter (ADR 0001)."""

    @staticmethod
    def _imports(path: Path) -> set[str]:
        """Return every module a source file imports."""
        found: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
                found.update(f"{node.module}.{alias.name}" for alias in node.names)
        return found

    @pytest.mark.parametrize("module", CORE_MODULES)
    def test_core_module_imports_no_adapter(self, module: str) -> None:
        """A core module may use the interface, never an adapter or the registry."""
        imports = self._imports(PACKAGE / f"{module}.py")

        offending = sorted(
            name
            for name in imports
            if name.startswith("grocery_butler.fulfillment")
            and not name.startswith("grocery_butler.fulfillment.base")
        )
        assert offending == []

    def test_the_interface_imports_no_adapter(self) -> None:
        """base.py stands alone, so adapters depend on it and not the reverse."""
        imports = self._imports(PACKAGE / "fulfillment" / "base.py")

        assert not any(
            name.startswith("grocery_butler.fulfillment") for name in imports
        )

    def test_every_core_module_listed_exists(self) -> None:
        """A renamed module can't silently drop out of the boundary check."""
        missing = [m for m in CORE_MODULES if not (PACKAGE / f"{m}.py").exists()]

        assert missing == []

    def test_no_retailer_name_appears_in_the_interface(self) -> None:
        """The interface is retailer-neutral in its words too."""
        text = (PACKAGE / "fulfillment" / "base.py").read_text().lower()

        assert not any(name in text for name in ("safeway", "instacart", "kroger"))
