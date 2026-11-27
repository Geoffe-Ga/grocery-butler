"""Versioned, deterministic shopping plans.

Implements ADR 0011. A plan is a numbered, immutable version built by plain
code from two inputs: the replenishment needs a member has approved, and,
optionally, the saved recipes a member wants to shop for. There is one line
per canonical household item, carrying every reason it is there. The same
inputs always give the same content and the same hash, and no model takes
part. Building the same thing twice stores nothing new.

An optional recipe ingredient is never planned on the builder's own
authority (ADR 0025). A build that names a recipe with optional ingredients
is refused until it carries a member's include-or-exclude decision for each
one. Nothing is stored until then, so an unconfirmed plan can't become the
current plan.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from grocery_butler.constraints import HouseholdRules
from grocery_butler.db import get_connection
from grocery_butler.db.adapter import IntegrityError
from grocery_butler.household_items import HouseholdItemStore
from grocery_butler.inventory_ledger import utc_now
from grocery_butler.models import (
    HouseholdItem,
    MealOptionalChoices,
    NeedReasonKind,
    NeedStatus,
    OptionalDecisionProblem,
    OptionalIngredientChoice,
    OptionalProblemKind,
    PlanAmount,
    PlanHardConstraint,
    PlanLine,
    PlanMeal,
    PlanReason,
    ShoppingPlan,
    StaplePolicy,
    UnplannedIngredient,
    coerce_category,
    parse_unit,
)
from grocery_butler.plan_preferences import line_preferences
from grocery_butler.replenishment import ReplenishmentNeeds
from grocery_butler.units import convert, dimension_of

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Callable, Sequence

    from grocery_butler.db.adapter import DatabaseConnection
    from grocery_butler.models import OptionalDecision, ReplenishmentNeed, Unit

# Needs a member has decided to buy. Suggested, deferred and closed needs
# are never planned.
_PLANNABLE = frozenset({NeedStatus.APPROVED, NeedStatus.PLANNED})

# Amounts are rounded when a line is finished, so unit conversion noise
# (2 cups plus a pint is 3.9999999958 cups) can't reach a plan or its hash.
AMOUNT_DECIMALS = 4

NEVER_SUGGEST_PROBLEM = (
    "This item's policy is never to suggest it, so the recipe did not add it."
    " Ask for it by name if you want it on the plan."
)
_KEPT_POLICIES = frozenset({StaplePolicy.ALWAYS_KEEP, StaplePolicy.REPLENISH_WHEN_LOW})
NO_ITEM_PROBLEM = (
    "This recipe ingredient is not linked to a household item, so it can't be "
    "planned. Save the recipe again to link it."
)

_SELECT_RECIPE = "SELECT display_name, default_servings FROM recipes WHERE id = ?"
_SELECT_RECIPE_INGREDIENTS = (
    "SELECT id, ingredient, quantity_per_serving, unit, category, notes,"
    " item_id, is_pantry_item, is_optional"
    " FROM recipe_ingredients WHERE recipe_id = ? ORDER BY id"
)

# The order problems are reported in. A repeated decision is wrong whatever
# the recipe says; a stale answer explains away the unknown and missing
# decisions that come with it; an unknown id is a mistake to fix before
# anything missing is asked for.
_PROBLEM_PRECEDENCE = (
    OptionalProblemKind.DUPLICATE,
    OptionalProblemKind.STALE,
    OptionalProblemKind.UNKNOWN,
    OptionalProblemKind.MISSING,
)
_NOTHING_PLANNED = " Nothing was planned."

_SELECT_PLAN_BY_VERSION = (
    "SELECT id, version, content_hash, meals, unplanned, created_by, created_at"
    " FROM shopping_plans WHERE version = ?"
)
_SELECT_ALL_PLANS = (
    "SELECT id, version, content_hash, meals, unplanned, created_by, created_at"
    " FROM shopping_plans ORDER BY version"
)
_SELECT_LINES = (
    "SELECT item_id, display_name, category, amounts, reasons, constraints,"
    " need_id, hard_constraints"
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


class OptionalDecisionsError(Exception):
    """Raised when a plan's optional ingredients are not properly decided.

    ``kind`` says what is wrong: decisions are ``missing``, name an
    ``unknown`` ingredient, ``duplicate`` one, or are ``stale`` because the
    recipe's optional ingredients changed after they were shown. ``meals``
    is the current question for every meal on the request that has optional
    ingredients, to show a member and answer. ``problems`` lists each fault
    of the kind reported.
    """

    def __init__(
        self,
        kind: OptionalProblemKind,
        message: str,
        *,
        meals: list[MealOptionalChoices],
        problems: list[OptionalDecisionProblem],
    ) -> None:
        """Describe the refusal.

        Args:
            kind: What is wrong with the decisions.
            message: What went wrong and what to do, for a person.
            meals: The current choices, one entry per meal that has any.
            problems: Each fault of this kind.
        """
        super().__init__(message)
        self.kind = kind
        self.meals = meals
        self.problems = problems


@dataclass(frozen=True)
class _MealInputs:
    """A requested meal with its recipe read and its servings settled."""

    meal: PlanMeal
    recipe: str
    rows: list[Any]
    choices: MealOptionalChoices


def _choices_token(
    recipe_id: int, servings: int, ingredients: list[OptionalIngredientChoice]
) -> str:
    """Fingerprint the question a meal's optional ingredients pose.

    Args:
        recipe_id: The recipe.
        servings: The servings the amounts were scaled to.
        ingredients: The optional ingredients, as they would be shown.

    Returns:
        A hex SHA-256 digest. It changes when any optional ingredient is
        added, removed or altered, or the servings change.
    """
    return hashlib.sha256(
        _dump(
            {
                "recipe_id": recipe_id,
                "servings": servings,
                "optional_ingredients": [
                    choice.model_dump(mode="json") for choice in ingredients
                ],
            }
        ).encode()
    ).hexdigest()


def _problems(
    recipe_id: int, ingredient_ids: Sequence[int | None], kind: OptionalProblemKind
) -> list[OptionalDecisionProblem]:
    """Build one problem record per ingredient.

    Args:
        recipe_id: The meal's recipe.
        ingredient_ids: The ingredient each problem names. None stands for
            a problem with the meal's answer as a whole.
        kind: What is wrong.

    Returns:
        The problems, in the order the ingredients were given.
    """
    return [
        OptionalDecisionProblem(
            recipe_id=recipe_id, ingredient_id=ingredient_id, problem=kind
        )
        for ingredient_id in ingredient_ids
    ]


def _answers_another_question(meal: PlanMeal, current_token: str) -> bool:
    """Say whether a meal's answer was given to a different question.

    Args:
        meal: The meal as it was sent.
        current_token: The token of the recipe's optional ingredients now.

    Returns:
        True when the meal carries decisions or a token, and the token is
        not the current one. A meal that answers nothing is not stale; it
        simply has not been asked yet.
    """
    answered = bool(meal.optional_decisions) or meal.optional_token is not None
    return answered and meal.optional_token != current_token


def _decision_problems(
    inputs: _MealInputs, *, check_token: bool
) -> list[OptionalDecisionProblem]:
    """Find everything wrong with the decisions sent for one meal.

    Args:
        inputs: The meal and its recipe's current optional ingredients.
        check_token: Whether the decisions must carry the current token.
            A stored plan's meals carry none.

    Returns:
        The problems, empty when every optional ingredient has exactly one
        decision and the decisions answer the current question.
    """
    meal = inputs.meal
    recipe_id = meal.recipe_id
    decided = [decision.ingredient_id for decision in meal.optional_decisions]
    expected = [c.ingredient_id for c in inputs.choices.optional_ingredients]
    repeated = sorted({i for i in decided if decided.count(i) > 1})
    duplicates = _problems(recipe_id, repeated, OptionalProblemKind.DUPLICATE)
    if check_token and _answers_another_question(meal, inputs.choices.optional_token):
        return [*duplicates, *_problems(recipe_id, [None], OptionalProblemKind.STALE)]
    unknown = sorted(set(decided) - set(expected))
    missing = [i for i in expected if i not in decided]
    return [
        *duplicates,
        *_problems(recipe_id, unknown, OptionalProblemKind.UNKNOWN),
        *_problems(recipe_id, missing, OptionalProblemKind.MISSING),
    ]


def _problem_sentence(
    kind: OptionalProblemKind, inputs: _MealInputs, ingredient_ids: list[int | None]
) -> str:
    """Say what is wrong with one meal's decisions, for a person.

    Args:
        kind: The kind of problem being reported.
        inputs: The meal the problems belong to.
        ingredient_ids: The ingredient each problem names.

    Returns:
        One or two sentences naming the recipe and the ingredients.
    """
    names = {
        choice.ingredient_id: choice.ingredient
        for choice in inputs.choices.optional_ingredients
    }
    recipe = f'"{inputs.recipe}"'
    listed = ", ".join(str(ingredient_id) for ingredient_id in ingredient_ids)
    if kind is OptionalProblemKind.DUPLICATE:
        return (
            f"{recipe} was sent more than one decision for ingredient {listed}."
            " Send exactly one decision for each optional ingredient."
        )
    if kind is OptionalProblemKind.STALE:
        return (
            f"The decisions for {recipe} do not answer its current optional"
            " ingredients: they changed after they were shown, or the meal's"
            " optional_token was left out. Show the member the current choices"
            " and send their decisions with the current optional_token."
        )
    if kind is OptionalProblemKind.UNKNOWN:
        return (
            f"{recipe} has no optional ingredient with id {listed}. Decide only"
            " the optional ingredients this recipe lists."
        )
    undecided = ", ".join(
        names[ingredient_id]
        for ingredient_id in ingredient_ids
        if ingredient_id is not None
    )
    return (
        f"{recipe} has optional ingredients nobody has decided on: {undecided}."
        " Ask the member, then send include true or false for each one, with"
        " the meal's optional_token."
    )


def _most_pressing(
    problems: list[OptionalDecisionProblem],
) -> list[OptionalDecisionProblem]:
    """Keep the problems of the one kind a refusal reports.

    Args:
        problems: All the problems found, of any kind. Not empty.

    Returns:
        The problems of the first kind in ``_PROBLEM_PRECEDENCE`` that
        occurs, in the order they were found.
    """
    found = {problem.problem for problem in problems}
    kind = next(kind for kind in _PROBLEM_PRECEDENCE if kind in found)
    return [problem for problem in problems if problem.problem is kind]


def _refusal(
    resolved: list[_MealInputs], problems: list[OptionalDecisionProblem]
) -> OptionalDecisionsError:
    """Build the refusal for a request whose decisions have problems.

    Args:
        resolved: Every meal on the request.
        problems: All the problems found, of any kind. Not empty.

    Returns:
        The error, reporting the most pressing kind of problem only.
    """
    reported = _most_pressing(problems)
    kind = reported[0].problem
    by_recipe: dict[int, list[int | None]] = {}
    for problem in reported:
        by_recipe.setdefault(problem.recipe_id, []).append(problem.ingredient_id)
    sentences = [
        _problem_sentence(kind, inputs, by_recipe[inputs.meal.recipe_id])
        for inputs in resolved
        if inputs.meal.recipe_id in by_recipe
    ]
    return OptionalDecisionsError(
        kind,
        " ".join(sentences) + _NOTHING_PLANNED,
        meals=[
            inputs.choices for inputs in resolved if inputs.choices.optional_ingredients
        ],
        problems=reported,
    )


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


def _kept_in_stock(item: HouseholdItem, legacy_staples: set[int]) -> bool:
    """Say whether an item is kept on hand, so a meal needn't buy it.

    The item's policy decides (ADR 0017). Only an item with no policy falls
    back to the older flat staple list.

    Args:
        item: The canonical item.
        legacy_staples: Canonical ids on the flat pantry staple list.

    Returns:
        True when a recipe's use of the item should not add it to a plan.
    """
    if item.staple_policy is None:
        return item.id in legacy_staples
    return item.staple_policy in _KEPT_POLICIES


def _target_amounts(item: HouseholdItem) -> list[PlanAmount]:
    """Return the amount to suggest for an item nothing stated an amount for.

    Args:
        item: The canonical item.

    Returns:
        The item's target amount, if it has one; otherwise nothing, which
        means "buy the usual".
    """
    if item.target_quantity is None or item.target_unit is None:
        return []
    return [PlanAmount(quantity=item.target_quantity, unit=item.target_unit)]


def _decision_order(decision: OptionalDecision) -> tuple[int, bool]:
    """Give a decision its place in a meal's stable order.

    Args:
        decision: One optional-ingredient decision.

    Returns:
        A key that sorts by ingredient, then by answer.
    """
    return (decision.ingredient_id, decision.include)


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
            "hard_constraints": json.loads(row["hard_constraints"]),
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
        self._rules = HouseholdRules(db_path, clock=clock)

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

    def optional_choices(self, meals: Sequence[PlanMeal]) -> list[MealOptionalChoices]:
        """List the optional ingredients a member must decide on.

        Args:
            meals: The saved recipes to shop for.

        Returns:
            One entry per meal that has optional ingredients, in recipe
            order, each with the token its decisions must carry. Empty when
            no meal has any.

        Raises:
            RecipeNotFoundError: If a meal names a recipe that doesn't exist.
            ValueError: If a recipe is listed more than once.
        """
        return [
            inputs.choices
            for inputs in self._resolve(meals)
            if inputs.choices.optional_ingredients
        ]

    def preview(self, meals: Sequence[PlanMeal]) -> ShoppingPlan:
        """Work out what a build would produce now, without storing it.

        Args:
            meals: The saved recipes to shop for. May be empty.

        Returns:
            An unsaved plan (``id`` and ``version`` are 0).

        Raises:
            RecipeNotFoundError: If a meal names a recipe that doesn't exist.
            ValueError: If a recipe is listed more than once.
            OptionalDecisionsError: If a meal's optional ingredients are
                not each decided, once, against the current token.
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
            A plan whose recorded optional decisions no longer cover its
            recipes' optional ingredients (one was added or removed) can't
            be rebuilt as it stands; it gets a fingerprint that matches no
            plan, which makes it stale.

        Raises:
            PlanNotFoundError: If there is no such version.
        """
        plan = self.get(version)
        if plan is None:
            raise PlanNotFoundError(version)
        try:
            return self._assemble(plan.meals, check_tokens=False).content_hash
        except OptionalDecisionsError as err:
            return hashlib.sha256(f"stale:{err.kind.value}".encode()).hexdigest()

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
            OptionalDecisionsError: If a meal's optional ingredients are
                not each decided, once, against the current token. Nothing
                is stored.
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

    def _assemble(
        self, meals: Sequence[PlanMeal], *, check_tokens: bool = True
    ) -> _Draft:
        """Assemble a plan's content from needs and meals.

        Args:
            meals: The saved recipes to shop for.
            check_tokens: Whether optional decisions must carry the current
                token. False when rebuilding a stored plan's own meals,
                which carry none.

        Returns:
            The content, with meals and lines in their stable order.

        Raises:
            RecipeNotFoundError: If a meal names a recipe that doesn't exist.
            ValueError: If a recipe is listed more than once.
            OptionalDecisionsError: If a meal's optional ingredients are
                not each decided exactly once.
        """
        resolved = self._resolve(meals)
        problems = [
            problem
            for inputs in resolved
            for problem in _decision_problems(inputs, check_token=check_tokens)
        ]
        if problems:
            raise _refusal(resolved, problems)

        demand: dict[int, _Demand] = {}
        for need in self._needs.list_needs(_PLANNABLE):
            self._add_need(demand, need)

        unplanned: list[UnplannedIngredient] = []
        staples = self._staple_item_ids()
        for inputs in resolved:
            self._add_meal(demand, unplanned, staples, inputs)

        lines = [self._line(entry) for entry in demand.values() if entry.reasons]
        lines.sort(key=lambda line: (str(line.category or ""), line.display_name))
        unplanned.sort(key=lambda entry: (entry.recipe_id, entry.ingredient))
        return _Draft(
            meals=[inputs.meal for inputs in resolved],
            lines=lines,
            unplanned=unplanned,
        )

    def _resolve(self, meals: Sequence[PlanMeal]) -> list[_MealInputs]:
        """Read each meal's recipe and settle its servings and choices.

        Args:
            meals: The saved recipes to shop for.

        Returns:
            The meals in recipe order. Each meal's decisions are sorted by
            ingredient, so the order they were sent in can't change a plan.

        Raises:
            RecipeNotFoundError: If a meal names a recipe that doesn't exist.
            ValueError: If a recipe is listed more than once.
        """
        recipe_ids = [meal.recipe_id for meal in meals]
        if len(set(recipe_ids)) != len(recipe_ids):
            msg = "List each recipe once, and say how many servings you want of it."
            raise ValueError(msg)
        return [
            self._meal_inputs(meal) for meal in sorted(meals, key=lambda m: m.recipe_id)
        ]

    def _meal_inputs(self, meal: PlanMeal) -> _MealInputs:
        """Read one meal's recipe and work out what it leaves to decide.

        Args:
            meal: The recipe and servings wanted, with any decisions.

        Returns:
            The meal with its servings filled in, its ingredient rows and
            its optional ingredients with the amounts the recipe states.

        Raises:
            RecipeNotFoundError: If the recipe does not exist.
        """
        with closing(get_connection(self._db_path)) as conn:
            recipe = conn.execute(_SELECT_RECIPE, (meal.recipe_id,)).fetchone()
            if recipe is None:
                raise RecipeNotFoundError(meal.recipe_id)
            rows = conn.execute(
                _SELECT_RECIPE_INGREDIENTS, (meal.recipe_id,)
            ).fetchall()
        servings = meal.servings or int(recipe["default_servings"] or 1)
        optional = [
            OptionalIngredientChoice(
                ingredient_id=row["id"],
                ingredient=row["ingredient"],
                quantity=round(
                    float(row["quantity_per_serving"]) * servings, AMOUNT_DECIMALS
                ),
                unit=parse_unit(row["unit"]),
                category=coerce_category(row["category"]),
                notes=row["notes"] or "",
                is_pantry_item=bool(row["is_pantry_item"]),
            )
            for row in rows
            if row["is_optional"]
        ]
        return _MealInputs(
            meal=PlanMeal(
                recipe_id=meal.recipe_id,
                servings=servings,
                optional_decisions=sorted(meal.optional_decisions, key=_decision_order),
                optional_token=meal.optional_token,
            ),
            recipe=recipe["display_name"],
            rows=rows,
            choices=MealOptionalChoices(
                recipe_id=meal.recipe_id,
                recipe=recipe["display_name"],
                servings=servings,
                optional_token=_choices_token(meal.recipe_id, servings, optional),
                optional_ingredients=optional,
            ),
        )

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
        inputs: _MealInputs,
    ) -> None:
        """Add one recipe's purchase ingredients to the demand.

        A required ingredient and an optional one a member included are
        treated alike. An optional ingredient a member excluded adds
        nothing.

        Args:
            demand: Demand gathered so far; updated in place.
            unplanned: Ingredients that can't be planned; appended to.
            staples: Canonical ids of the household's pantry staples.
            inputs: The meal, with its decisions checked, and its recipe.
        """
        meal = inputs.meal
        servings = inputs.choices.servings
        excluded = {
            decision.ingredient_id
            for decision in meal.optional_decisions
            if not decision.include
        }
        for row in inputs.rows:
            if row["is_pantry_item"] or row["id"] in excluded:
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
            if _kept_in_stock(entry.item, staples):
                continue
            if entry.item.staple_policy is StaplePolicy.NEVER_SUGGEST:
                unplanned.append(
                    UnplannedIngredient(
                        recipe_id=meal.recipe_id,
                        ingredient=row["ingredient"],
                        problem=NEVER_SUGGEST_PROBLEM,
                    )
                )
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
                        "recipe": inputs.recipe,
                        "quantity": quantity,
                        "unit": unit.value,
                        "servings": servings,
                    },
                )
            )

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

    def _line(self, entry: _Demand) -> PlanLine:
        """Turn an item's gathered demand into a plan line.

        Args:
            entry: The item's demand.

        Returns:
            The line, with reasons and amounts in a stable order and every
            hard constraint that applies to the item (ADR 0012).
        """
        item = entry.item
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
                    for amount in entry.amounts or _target_amounts(item)
                ),
                key=lambda amount: amount.unit.value,
            ),
            reasons=sorted(entry.reasons, key=lambda r: (r.kind.value, r.source_key)),
            constraints=line_preferences(
                self._rules.soft_for_item(item.id), item.verified_upc
            ),
            need_id=entry.need_id,
            hard_constraints=[
                PlanHardConstraint(
                    kind=constraint.kind,
                    value=constraint.value,
                    scope=constraint.scope,
                )
                for constraint in self._rules.hard_for_item(item.id)
            ],
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
                        "  amounts, reasons, constraints, need_id, hard_constraints)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                            _dump(line["hard_constraints"]),
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
