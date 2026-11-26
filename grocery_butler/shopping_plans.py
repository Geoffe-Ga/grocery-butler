"""Versioned, deterministic shopping plans.

Implements ADR 0011. A plan is a numbered, immutable version built by plain
code from two inputs: the replenishment needs a member has approved, and,
optionally, the saved recipes a member wants to shop for. There is one line
per canonical household item, carrying every reason it is there. The same
inputs always give the same content and the same hash, and no model takes
part. Building the same thing twice stores nothing new.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from grocery_butler.db import get_connection
from grocery_butler.db.adapter import IntegrityError
from grocery_butler.household_items import HouseholdItemStore
from grocery_butler.inventory_ledger import utc_now
from grocery_butler.models import (
    HouseholdItem,
    NeedReasonKind,
    NeedStatus,
    PlanAmount,
    PlanLine,
    PlanMeal,
    PlanReason,
    ShoppingPlan,
    UnplannedIngredient,
    parse_unit,
)
from grocery_butler.replenishment import ReplenishmentNeeds
from grocery_butler.units import convert, dimension_of

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Callable, Sequence

    from grocery_butler.db.adapter import DatabaseConnection
    from grocery_butler.models import ReplenishmentNeed, Unit

# Needs a member has decided to buy. Suggested, deferred and closed needs
# are never planned.
_PLANNABLE = frozenset({NeedStatus.APPROVED, NeedStatus.PLANNED})

# Amounts are rounded when a line is finished, so unit conversion noise
# (2 cups plus a pint is 3.9999999958 cups) can't reach a plan or its hash.
AMOUNT_DECIMALS = 4

NO_ITEM_PROBLEM = (
    "This recipe ingredient is not linked to a household item, so it can't be "
    "planned. Save the recipe again to link it."
)

_SELECT_PLAN_BY_VERSION = (
    "SELECT id, version, content_hash, meals, unplanned, created_by, created_at"
    " FROM shopping_plans WHERE version = ?"
)
_SELECT_ALL_PLANS = (
    "SELECT id, version, content_hash, meals, unplanned, created_by, created_at"
    " FROM shopping_plans ORDER BY version"
)
_SELECT_LINES = (
    "SELECT item_id, display_name, category, amounts, reasons, constraints, need_id"
    " FROM shopping_plan_lines WHERE plan_id = ? ORDER BY position"
)


class PlanNotFoundError(LookupError):
    """Raised when a shopping plan version does not exist."""

    def __init__(self, version: int) -> None:
        """Name the missing version.

        Args:
            version: The version that was looked up.
        """
        super().__init__(f"There is no shopping plan version {version}.")
        self.version = version


class RecipeNotFoundError(LookupError):
    """Raised when a plan names a recipe that does not exist."""

    def __init__(self, recipe_id: int) -> None:
        """Name the missing recipe.

        Args:
            recipe_id: The recipe id that was looked up.
        """
        super().__init__(f"No saved recipe has id {recipe_id}.")
        self.recipe_id = recipe_id


@dataclass
class _Draft:
    """A plan's content before it is stored."""

    meals: list[PlanMeal]
    lines: list[PlanLine]
    unplanned: list[UnplannedIngredient]

    def content(self) -> dict[str, Any]:
        """Return the content that defines the plan.

        Returns:
            The meals, lines and unplanned ingredients as plain JSON data.
        """
        return {
            "meals": [meal.model_dump(mode="json") for meal in self.meals],
            "lines": [line.model_dump(mode="json") for line in self.lines],
            "unplanned": [entry.model_dump(mode="json") for entry in self.unplanned],
        }

    @property
    def content_hash(self) -> str:
        """Hash the plan's content.

        Returns:
            The hex SHA-256 digest of the canonical JSON content.
        """
        text = json.dumps(self.content(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class _Demand:
    """Everything wanted of one canonical item while a plan is assembled."""

    item: HouseholdItem
    reasons: list[PlanReason] = field(default_factory=list)
    amounts: list[PlanAmount] = field(default_factory=list)
    need_id: int | None = None


def _dump(value: object) -> str:
    """Serialize plan data as canonical JSON text.

    Args:
        value: JSON-serializable data.

    Returns:
        JSON with sorted keys and no optional whitespace.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _add_amount(amounts: list[PlanAmount], quantity: float, unit: Unit) -> None:
    """Add an amount, summing it into one of the same unit dimension.

    Cups are added to gallons. A jar is never added to cups; it becomes a
    separate amount on the same line.

    Args:
        amounts: The line's amounts so far; updated in place.
        quantity: The amount to add.
        unit: Its unit.
    """
    dimension = dimension_of(unit)
    for index, existing in enumerate(amounts):
        same_group = (
            dimension_of(existing.unit) is dimension
            if dimension is not None
            else existing.unit is unit
        )
        converted = convert(quantity, unit, existing.unit)
        if same_group and converted is not None:
            amounts[index] = PlanAmount(
                quantity=existing.quantity + converted, unit=existing.unit
            )
            return
    amounts.append(PlanAmount(quantity=quantity, unit=unit))


def _row_to_line(row: Any) -> PlanLine:
    """Build a PlanLine from a stored row.

    Args:
        row: A row from the lines SELECT statement.

    Returns:
        The line.
    """
    return PlanLine.model_validate(
        {
            "item_id": row["item_id"],
            "display_name": row["display_name"],
            "category": row["category"],
            "amounts": json.loads(row["amounts"]),
            "reasons": json.loads(row["reasons"]),
            "constraints": json.loads(row["constraints"]),
            "need_id": row["need_id"],
        }
    )


class ShoppingPlans:
    """Build and read versions of the household's shopping plan.

    Opens a connection per operation. The schema must already be migrated.

    Args:
        db_path: SQLite file path or PostgreSQL URL.
        clock: Returns the current UTC time. Tests pass a fixed clock.
    """

    def __init__(
        self, db_path: str, clock: Callable[[], dt.datetime] = utc_now
    ) -> None:
        """Initialize the service.

        Args:
            db_path: SQLite file path or PostgreSQL URL.
            clock: Returns the current UTC time.
        """
        self._db_path = db_path
        self._clock = clock
        self._items = HouseholdItemStore(db_path)
        self._needs = ReplenishmentNeeds(db_path, clock=clock)

    # -- reads ---------------------------------------------------------

    def get(self, version: int) -> ShoppingPlan | None:
        """Return one stored version.

        Args:
            version: The plan version.

        Returns:
            The plan, or None if there is no such version.
        """
        with closing(get_connection(self._db_path)) as conn:
            row = conn.execute(_SELECT_PLAN_BY_VERSION, (version,)).fetchone()
            return self._load(conn, row) if row else None

    def latest(self) -> ShoppingPlan | None:
        """Return the current plan: the highest version.

        Returns:
            The latest plan, or None when no plan was ever built.
        """
        plans = self.list_plans()
        return plans[-1] if plans else None

    def list_plans(self) -> list[ShoppingPlan]:
        """List every stored version, oldest first.

        Returns:
            All plan versions with their lines.
        """
        with closing(get_connection(self._db_path)) as conn:
            rows = conn.execute(_SELECT_ALL_PLANS).fetchall()
            return [self._load(conn, row) for row in rows]

    def preview(self, meals: Sequence[PlanMeal]) -> ShoppingPlan:
        """Work out what a build would produce now, without storing it.

        Args:
            meals: The saved recipes to shop for. May be empty.

        Returns:
            An unsaved plan (``id`` and ``version`` are 0).

        Raises:
            RecipeNotFoundError: If a meal names a recipe that doesn't exist.
            ValueError: If a recipe is listed more than once.
        """
        draft = self._assemble(meals)
        return ShoppingPlan(
            id=0,
            version=0,
            content_hash=draft.content_hash,
            meals=draft.meals,
            lines=draft.lines,
            unplanned=draft.unplanned,
            created_by="",
            created_at=self._clock(),
        )

    def current_fingerprint(self, version: int) -> str:
        """Hash what a plan's own inputs would produce now.

        This is the fingerprint an approval envelope for the plan must
        match (ADR 0010): it equals the plan's ``content_hash`` until the
        needs, recipes or items under it change.

        Args:
            version: The plan version.

        Returns:
            The content hash a fresh build of the plan's meals would have.

        Raises:
            PlanNotFoundError: If there is no such version.
        """
        plan = self.get(version)
        if plan is None:
            raise PlanNotFoundError(version)
        return self._assemble(plan.meals).content_hash

    # -- writes --------------------------------------------------------

    def build(self, meals: Sequence[PlanMeal], *, actor: str) -> ShoppingPlan:
        """Build the plan and store it as a new version if anything changed.

        If the content is identical to the latest version, that version is
        returned and nothing is stored. Otherwise the next version is
        stored and its approved needs move to ``planned``.

        Args:
            meals: The saved recipes to shop for. May be empty.
            actor: Who asked for the plan.

        Returns:
            The current plan.

        Raises:
            RecipeNotFoundError: If a meal names a recipe that doesn't exist.
            ValueError: If a recipe is listed more than once.
        """
        draft = self._assemble(meals)
        latest = self.latest()
        if latest is not None and latest.content_hash == draft.content_hash:
            return latest
        version = self._store(draft, latest.version + 1 if latest else 1, actor)
        for line in draft.lines:
            self._mark_planned(line, actor)
        stored = self.get(version)
        if stored is None:  # pragma: no cover - just stored
            raise PlanNotFoundError(version)
        return stored

    # -- assembly ------------------------------------------------------

    def _assemble(self, meals: Sequence[PlanMeal]) -> _Draft:
        """Assemble a plan's content from needs and meals.

        Args:
            meals: The saved recipes to shop for.

        Returns:
            The content, with meals and lines in their stable order.

        Raises:
            RecipeNotFoundError: If a meal names a recipe that doesn't exist.
            ValueError: If a recipe is listed more than once.
        """
        recipe_ids = [meal.recipe_id for meal in meals]
        if len(set(recipe_ids)) != len(recipe_ids):
            msg = "List each recipe once, and say how many servings you want of it."
            raise ValueError(msg)

        demand: dict[int, _Demand] = {}
        for need in self._needs.list_needs(_PLANNABLE):
            self._add_need(demand, need)

        resolved: list[PlanMeal] = []
        unplanned: list[UnplannedIngredient] = []
        staples = self._staple_item_ids()
        for meal in sorted(meals, key=lambda m: m.recipe_id):
            resolved.append(self._add_meal(demand, unplanned, staples, meal))

        lines = [self._line(entry) for entry in demand.values() if entry.reasons]
        lines.sort(key=lambda line: (str(line.category or ""), line.display_name))
        unplanned.sort(key=lambda entry: (entry.recipe_id, entry.ingredient))
        return _Draft(meals=resolved, lines=lines, unplanned=unplanned)

    def _entry(self, demand: dict[int, _Demand], item_id: int) -> _Demand:
        """Find or start the demand entry of an item's canonical item.

        Args:
            demand: Demand gathered so far, keyed by canonical item id.
            item_id: Any item id; a merged id is followed to its target.

        Returns:
            The canonical item's entry.
        """
        item = self._items.canonical(item_id)
        if item.id not in demand:
            demand[item.id] = _Demand(item=item)
        return demand[item.id]

    def _add_need(self, demand: dict[int, _Demand], need: ReplenishmentNeed) -> None:
        """Add an approved need's active reasons to its item's demand.

        Args:
            demand: Demand gathered so far; updated in place.
            need: An approved or planned need.
        """
        entry = self._entry(demand, need.item_id)
        entry.need_id = need.id
        entry.reasons.extend(
            PlanReason(kind=r.kind, source_key=r.source_key, detail=r.detail)
            for r in need.active_reasons
        )

    def _add_meal(
        self,
        demand: dict[int, _Demand],
        unplanned: list[UnplannedIngredient],
        staples: set[int],
        meal: PlanMeal,
    ) -> PlanMeal:
        """Add one recipe's purchase ingredients to the demand.

        Args:
            demand: Demand gathered so far; updated in place.
            unplanned: Ingredients that can't be planned; appended to.
            staples: Canonical ids of the household's pantry staples.
            meal: The recipe and servings wanted.

        Returns:
            The meal with its servings filled in.

        Raises:
            RecipeNotFoundError: If the recipe does not exist.
        """
        with closing(get_connection(self._db_path)) as conn:
            recipe = conn.execute(
                "SELECT display_name, default_servings FROM recipes WHERE id = ?",
                (meal.recipe_id,),
            ).fetchone()
            if recipe is None:
                raise RecipeNotFoundError(meal.recipe_id)
            rows = conn.execute(
                "SELECT ingredient, quantity_per_serving, unit, item_id, is_pantry_item"
                " FROM recipe_ingredients WHERE recipe_id = ? ORDER BY id",
                (meal.recipe_id,),
            ).fetchall()
        servings = meal.servings or int(recipe["default_servings"] or 1)
        for row in rows:
            if row["is_pantry_item"]:
                continue
            if row["item_id"] is None:
                unplanned.append(
                    UnplannedIngredient(
                        recipe_id=meal.recipe_id,
                        ingredient=row["ingredient"],
                        problem=NO_ITEM_PROBLEM,
                    )
                )
                continue
            entry = self._entry(demand, row["item_id"])
            if entry.item.id in staples:
                continue
            unit = parse_unit(row["unit"])
            quantity = round(
                float(row["quantity_per_serving"]) * servings, AMOUNT_DECIMALS
            )
            _add_amount(entry.amounts, quantity, unit)
            entry.reasons.append(
                PlanReason(
                    kind=NeedReasonKind.RECIPE_DEMAND,
                    source_key=f"recipe:{meal.recipe_id}",
                    detail={
                        "recipe": recipe["display_name"],
                        "quantity": quantity,
                        "unit": unit.value,
                        "servings": servings,
                    },
                )
            )
        return PlanMeal(recipe_id=meal.recipe_id, servings=servings)

    def _staple_item_ids(self) -> set[int]:
        """Return the canonical item ids of the household's pantry staples.

        Returns:
            Items assumed on hand unless a need says otherwise.
        """
        with closing(get_connection(self._db_path)) as conn:
            rows = conn.execute(
                "SELECT item_id FROM pantry_staples WHERE item_id IS NOT NULL"
            ).fetchall()
        return {self._items.canonical(row["item_id"]).id for row in rows}

    @staticmethod
    def _line(entry: _Demand) -> PlanLine:
        """Turn an item's gathered demand into a plan line.

        Args:
            entry: The item's demand.

        Returns:
            The line, with reasons and amounts in a stable order.
        """
        item = entry.item
        constraints = {
            key: value
            for key, value in (
                ("preferred_brand", item.preferred_brand),
                ("preferred_size", item.preferred_size),
            )
            if value
        }
        return PlanLine(
            item_id=item.id,
            display_name=item.display_name,
            category=item.category,
            amounts=sorted(
                (
                    PlanAmount(
                        quantity=round(amount.quantity, AMOUNT_DECIMALS),
                        unit=amount.unit,
                    )
                    for amount in entry.amounts
                ),
                key=lambda amount: amount.unit.value,
            ),
            reasons=sorted(entry.reasons, key=lambda r: (r.kind.value, r.source_key)),
            constraints=constraints,
            need_id=entry.need_id,
        )

    # -- storage -------------------------------------------------------

    def _store(self, draft: _Draft, version: int, actor: str) -> int:
        """Store a plan version and its lines in one transaction.

        If another builder stored the same version number first, the next
        number is used instead.

        Args:
            draft: The content to store.
            version: The version number to try first.
            actor: Who asked for the plan.

        Returns:
            The version number the plan was stored under.
        """
        content = draft.content()
        try:
            with closing(get_connection(self._db_path)) as conn:
                cursor = conn.execute(
                    "INSERT INTO shopping_plans"
                    " (version, content_hash, meals, unplanned, created_by,"
                    "  created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        version,
                        draft.content_hash,
                        _dump(content["meals"]),
                        _dump(content["unplanned"]),
                        actor,
                        self._clock().isoformat(),
                    ),
                )
                for position, line in enumerate(content["lines"]):
                    conn.execute(
                        "INSERT INTO shopping_plan_lines"
                        " (plan_id, position, item_id, display_name, category,"
                        "  amounts, reasons, constraints, need_id)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            cursor.lastrowid,
                            position,
                            line["item_id"],
                            line["display_name"],
                            line["category"],
                            _dump(line["amounts"]),
                            _dump(line["reasons"]),
                            _dump(line["constraints"]),
                            line["need_id"],
                        ),
                    )
                conn.commit()
        except IntegrityError:
            return self._store(draft, version + 1, actor)
        return version

    def _mark_planned(self, line: PlanLine, actor: str) -> None:
        """Move the approved need behind a stored line to ``planned``.

        Args:
            line: A line of the version just stored.
            actor: Who asked for the plan.
        """
        if line.need_id is None:
            return
        need = self._needs.get(line.need_id)
        if need is not None and need.status is NeedStatus.APPROVED:
            self._needs.transition(
                need.id,
                NeedStatus.PLANNED,
                actor=actor,
                note="Included in a shopping plan.",
            )

    def _load(self, conn: DatabaseConnection, row: Any) -> ShoppingPlan:
        """Build a stored plan from its row and its lines.

        Args:
            conn: Open connection.
            row: A row from one of the plan SELECT statements.

        Returns:
            The plan.
        """
        lines = conn.execute(_SELECT_LINES, (row["id"],)).fetchall()
        return ShoppingPlan.model_validate(
            {
                "id": row["id"],
                "version": row["version"],
                "content_hash": row["content_hash"],
                "meals": json.loads(row["meals"]),
                "lines": [_row_to_line(line) for line in lines],
                "unplanned": json.loads(row["unplanned"]),
                "created_by": row["created_by"],
                "created_at": row["created_at"],
            }
        )
