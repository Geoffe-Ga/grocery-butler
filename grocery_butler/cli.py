"""Command-line interface for Grocery Butler.

Provides subcommands for meal planning, inventory management,
restock queue, recipe management, and pantry staple management.
Wires together MealParser, Consolidator, PantryManager, and RecipeStore
into a fully functional terminal-based meal planning tool.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import TYPE_CHECKING

from grocery_butler.consolidator import Consolidator
from grocery_butler.meal_parser import MealParser
from grocery_butler.models import (
    CartItem,
    CartSummary,
    IngredientCategory,
    InventoryItem,
    InventoryStatus,
    ShoppingListItem,
    Unit,
)
from grocery_butler.pantry_manager import PantryManager
from grocery_butler.recipe_store import RecipeStore
from grocery_butler.safeway_pipeline import SafewayPipeline, SafewayPipelineError

if TYPE_CHECKING:
    from grocery_butler.config import Config


logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Config + dependency bootstrap
# ------------------------------------------------------------------


def _load_config_safe() -> Config | None:
    """Load application config, returning None on failure.

    Returns:
        Config instance or None if loading fails.
    """
    try:
        from grocery_butler.config import load_config

        return load_config()
    except Exception:
        return None


def _make_anthropic_client(api_key: str) -> object | None:
    """Create an Anthropic client from the given API key.

    Args:
        api_key: Anthropic API key string.

    Returns:
        Anthropic client instance or None on import failure.
    """
    try:
        import anthropic

        return anthropic.Anthropic(api_key=api_key)
    except Exception:
        logger.warning("Anthropic client unavailable; Claude features disabled")
        return None


# ------------------------------------------------------------------
# Output formatting
# ------------------------------------------------------------------

_CATEGORY_DISPLAY: dict[str, str] = {
    "produce": "Produce",
    "meat": "Meat & Seafood",
    "dairy": "Dairy",
    "bakery": "Bakery",
    "pantry_dry": "Pantry & Dry Goods",
    "frozen": "Frozen",
    "beverages": "Beverages",
    "deli": "Deli",
    "other": "Other",
}


def _format_shopping_list(items: list[ShoppingListItem]) -> str:
    """Format a shopping list grouped by category with aligned columns.

    Args:
        items: List of ShoppingListItem to format.

    Returns:
        Formatted shopping list string.
    """
    if not items:
        return "Shopping list is empty."

    regular: list[ShoppingListItem] = []
    restock: list[ShoppingListItem] = []
    for item in items:
        if "restock" in item.from_meals:
            restock.append(item)
        else:
            regular.append(item)

    lines: list[str] = []
    if regular:
        lines.append(_format_items_by_category(regular))
    if restock:
        if lines:
            lines.append("")
        lines.append("--- Restock Items ---")
        lines.append(_format_items_by_category(restock))

    return "\n".join(lines)


def _format_items_by_category(items: list[ShoppingListItem]) -> str:
    """Format shopping list items grouped by category.

    Args:
        items: Items to format.

    Returns:
        Formatted string with category headers and aligned items.
    """
    by_cat: dict[str, list[ShoppingListItem]] = {}
    for item in items:
        cat_key = str(item.category)
        by_cat.setdefault(cat_key, []).append(item)

    lines: list[str] = []
    for cat_key in sorted(by_cat.keys()):
        display = _CATEGORY_DISPLAY.get(cat_key, cat_key.replace("_", " ").title())
        lines.append(f"[{display}]")
        for item in sorted(by_cat[cat_key], key=lambda x: x.ingredient):
            qty = _format_quantity(item.quantity)
            lines.append(f"  {qty} {item.unit:<8s} {item.ingredient}")
        lines.append("")

    return "\n".join(lines).rstrip()


def _format_quantity(qty: float) -> str:
    """Format a numeric quantity for display.

    Shows integers without decimals and floats to 1 decimal.

    Args:
        qty: Quantity value.

    Returns:
        Formatted quantity string right-aligned in 6 chars.
    """
    if qty == int(qty):
        return f"{int(qty):>6d}"
    return f"{qty:>6.1f}"


def _format_inventory(items: list[InventoryItem]) -> str:
    """Format inventory items with status indicators.

    Args:
        items: List of inventory items.

    Returns:
        Formatted inventory string.
    """
    if not items:
        return "No tracked inventory items."

    lines: list[str] = []
    for item in items:
        tag = f"[{item.status.value.upper()}]"
        qty_str = ""
        if item.current_quantity is not None and item.current_unit is not None:
            qty_str = f"  ({item.current_quantity:g} {item.current_unit})"
        lines.append(f"  {tag:<10s} {item.display_name}{qty_str}")
    return "\n".join(lines)


def _format_recipes(recipes: list[dict[str, object]]) -> str:
    """Format recipe list as a table with name, times ordered.

    Args:
        recipes: List of recipe summary dicts.

    Returns:
        Formatted table string.
    """
    if not recipes:
        return "No saved recipes."

    header = f"  {'Name':<30s} {'Ordered':>8s}"
    sep = "  " + "-" * 40
    lines: list[str] = [header, sep]
    for recipe in recipes:
        name = str(recipe.get("display_name", ""))
        times = str(recipe.get("times_ordered", 0))
        lines.append(f"  {name:<30s} {times:>8s}")
    return "\n".join(lines)


def _format_pantry_staples(staples: list[dict[str, object]]) -> str:
    """Format pantry staples as a table with name and category.

    Args:
        staples: List of pantry staple dicts.

    Returns:
        Formatted table string.
    """
    if not staples:
        return "No pantry staples configured."

    header = f"  {'Name':<25s} {'Category':<15s}"
    sep = "  " + "-" * 42
    lines: list[str] = [header, sep]
    for staple in staples:
        name = str(staple.get("display_name", ""))
        cat = str(staple.get("category", ""))
        lines.append(f"  {name:<25s} {cat:<15s}")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Subcommand handlers
# ------------------------------------------------------------------


def _handle_plan(args: argparse.Namespace) -> int:
    """Handle the ``plan`` subcommand.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    cfg = _load_config_safe()
    if cfg is None:
        print(
            "Error: Missing ANTHROPIC_API_KEY. "
            "Copy .env.example to .env and fill in your key.",
            file=sys.stderr,
        )
        return 1

    client = _make_anthropic_client(cfg.anthropic_api_key)
    store = RecipeStore(cfg.database_path)
    pantry_mgr = PantryManager(cfg.database_path, anthropic_client=client)
    parser = MealParser(store, anthropic_client=client, config=cfg)
    consolidator = Consolidator(anthropic_client=client, config=cfg)

    meal_names = [m.strip() for m in args.meals.split(",") if m.strip()]
    if not meal_names:
        print("Error: No meals specified.", file=sys.stderr)
        return 1

    servings: int | None = args.servings

    try:
        parsed_meals = parser.parse_meals(meal_names, servings=servings)
    except Exception as exc:
        print(f"Error parsing meals: {exc}", file=sys.stderr)
        print("Hint: Check your API key and try again.", file=sys.stderr)
        return 1

    if args.save:
        for meal in parsed_meals:
            if not meal.known_recipe:
                parser.save_parsed_meal(meal)
                print(f"Saved recipe: {meal.name}")

    restock_queue = pantry_mgr.get_restock_queue()
    pantry_staple_names = store.get_pantry_staple_names()

    try:
        shopping_list = consolidator.consolidate(
            parsed_meals,
            restock_queue,
            pantry_staple_names,
        )
    except Exception as exc:
        print(f"Error consolidating shopping list: {exc}", file=sys.stderr)
        print("Hint: Check your API key and try again.", file=sys.stderr)
        return 1

    print(_format_shopping_list(shopping_list))
    return 0


def _handle_stock(args: argparse.Namespace) -> int:
    """Handle the ``stock`` subcommand.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    cfg = _load_config_safe()
    db_path = cfg.database_path if cfg else "mealbot.db"
    pantry_mgr = PantryManager(db_path)

    action: str | None = args.action

    if action is None:
        items = pantry_mgr.get_inventory()
        print(_format_inventory(items))
        return 0

    item_name: str = args.item_name

    if action == "add":
        category_str: str = args.category
        try:
            cat = IngredientCategory(category_str)
        except ValueError:
            valid = ", ".join(c.value for c in IngredientCategory)
            print(
                f"Error: Invalid category '{category_str}'. Valid: {valid}",
                file=sys.stderr,
            )
            return 1
        item = InventoryItem(
            ingredient=item_name.lower(),
            display_name=item_name.strip().title(),
            category=cat,
            status=InventoryStatus.ON_HAND,
        )
        pantry_mgr.add_item(item)
        print(f"Added '{item.display_name}' to inventory.")
        return 0

    status_map: dict[str, InventoryStatus] = {
        "out": InventoryStatus.OUT,
        "low": InventoryStatus.LOW,
        "good": InventoryStatus.ON_HAND,
    }
    new_status = status_map.get(action)
    if new_status is None:
        print(f"Error: Unknown action '{action}'.", file=sys.stderr)
        return 1

    existing = pantry_mgr.get_item(item_name)
    if existing is None:
        print(f"Error: '{item_name}' not found in inventory.", file=sys.stderr)
        return 1

    pantry_mgr.update_status(item_name, new_status)

    qty: float | None = getattr(args, "quantity", None)
    unit: str | None = getattr(args, "unit", None)
    if qty is not None and unit is not None:
        pantry_mgr.update_quantity(item_name, qty, unit)
        print(f"Updated '{item_name}' to {new_status.value} ({qty:g} {unit}).")
    else:
        print(f"Updated '{item_name}' to {new_status.value}.")
    return 0


def _handle_restock(args: argparse.Namespace) -> int:
    """Handle the ``restock`` subcommand.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    cfg = _load_config_safe()
    db_path = cfg.database_path if cfg else "mealbot.db"
    pantry_mgr = PantryManager(db_path)

    action: str | None = args.action

    if action == "clear":
        count = pantry_mgr.clear_restock_queue()
        print(f"Cleared {count} item(s) from restock queue.")
        return 0

    items = pantry_mgr.get_restock_queue()
    if not items:
        print("Restock queue is empty.")
        return 0

    print("Restock Queue:")
    print(_format_inventory(items))
    return 0


def _handle_recipes(args: argparse.Namespace) -> int:
    """Handle the ``recipes`` subcommand.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    cfg = _load_config_safe()
    db_path = cfg.database_path if cfg else "mealbot.db"
    store = RecipeStore(db_path)

    action: str | None = args.action

    if action is None:
        recipes = store.list_recipes()
        print(_format_recipes(recipes))
        return 0

    recipe_name: str = args.recipe_name

    if action == "show":
        meal = store.find_recipe(recipe_name)
        if meal is None:
            print(f"Recipe '{recipe_name}' not found.", file=sys.stderr)
            return 1
        print(f"Recipe: {meal.name} ({meal.servings} servings)")
        print("Purchase items:")
        for item in meal.purchase_items:
            qty = _format_quantity(item.quantity).strip()
            print(f"  {qty} {item.unit} {item.ingredient}")
        print("Pantry items:")
        for item in meal.pantry_items:
            qty = _format_quantity(item.quantity).strip()
            print(f"  {qty} {item.unit} {item.ingredient}")
        return 0

    if action == "forget":
        return _forget_recipe(store, recipe_name)

    print(f"Error: Unknown action '{action}'.", file=sys.stderr)
    return 1


def _forget_recipe(store: RecipeStore, recipe_name: str) -> int:
    """Delete a recipe by name.

    Args:
        store: RecipeStore instance.
        recipe_name: Name of recipe to delete.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    recipes = store.list_recipes()
    from grocery_butler.recipe_store import normalize_recipe_name

    normalized = normalize_recipe_name(recipe_name)
    for recipe in recipes:
        if normalize_recipe_name(str(recipe["display_name"])) == normalized:
            recipe_id = recipe.get("id")
            if recipe_id is not None:
                store.delete_recipe(int(str(recipe_id)))
                print(f"Deleted recipe '{recipe['display_name']}'.")
                return 0
    # Try substring match
    for recipe in recipes:
        name_norm = normalize_recipe_name(str(recipe["display_name"]))
        if normalized in name_norm:
            recipe_id = recipe.get("id")
            if recipe_id is not None:
                store.delete_recipe(int(str(recipe_id)))
                print(f"Deleted recipe '{recipe['display_name']}'.")
                return 0
    print(f"Recipe '{recipe_name}' not found.", file=sys.stderr)
    return 1


def _handle_pantry(args: argparse.Namespace) -> int:
    """Handle the ``pantry`` subcommand.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    cfg = _load_config_safe()
    db_path = cfg.database_path if cfg else "mealbot.db"
    store = RecipeStore(db_path)

    action: str | None = args.action

    if action is None:
        staples = store.get_pantry_staples()
        print(_format_pantry_staples(staples))
        return 0

    if action == "add":
        ingredient_name: str = args.ingredient_name
        category_str: str = args.category
        try:
            IngredientCategory(category_str)
        except ValueError:
            valid = ", ".join(c.value for c in IngredientCategory)
            print(
                f"Error: Invalid category '{category_str}'. Valid: {valid}",
                file=sys.stderr,
            )
            return 1
        store.add_pantry_staple(ingredient_name, category_str)
        print(f"Added '{ingredient_name}' as a pantry staple.")
        return 0

    if action == "remove":
        ingredient_name = args.ingredient_name
        return _remove_pantry_staple(store, ingredient_name)

    print(f"Error: Unknown action '{action}'.", file=sys.stderr)
    return 1


def _remove_pantry_staple(store: RecipeStore, ingredient_name: str) -> int:
    """Remove a pantry staple by name.

    Args:
        store: RecipeStore instance.
        ingredient_name: Name of the staple to remove.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    staples = store.get_pantry_staples()
    target = ingredient_name.strip().lower()
    for staple in staples:
        if str(staple.get("ingredient", "")).lower() == target:
            staple_id = staple.get("id")
            if staple_id is not None:
                store.remove_pantry_staple(int(str(staple_id)))
                print(f"Removed '{ingredient_name}' from pantry staples.")
                return 0
    print(f"Pantry staple '{ingredient_name}' not found.", file=sys.stderr)
    return 1


def _format_cart_items(
    header: str,
    items: list[CartItem],
) -> list[str]:
    """Format a section of cart items for display.

    Args:
        header: Section header text.
        items: CartItem instances to format.

    Returns:
        Lines for this section.
    """
    if not items:
        return []
    lines = [header]
    for ci in items:
        lines.append(
            f"  {ci.quantity_to_order}x {ci.safeway_product.name}"
            f"  ${ci.estimated_cost:.2f}"
        )
    lines.append("")
    return lines


def _format_cart_summary(cart: CartSummary) -> str:
    """Format a CartSummary for terminal display.

    Args:
        cart: CartSummary instance.

    Returns:
        Formatted cart summary string.
    """
    if not isinstance(cart, CartSummary):
        return "Invalid cart summary."

    lines: list[str] = ["=== Cart Summary ===", ""]
    lines.extend(_format_cart_items(f"Items ({len(cart.items)}):", cart.items))
    lines.extend(
        _format_cart_items(
            f"Restock Items ({len(cart.restock_items)}):", cart.restock_items
        )
    )

    if cart.failed_items:
        lines.append(f"Failed ({len(cart.failed_items)}):")
        for item in cart.failed_items:
            lines.append(f"  - {item.ingredient}")
        lines.append("")

    if cart.substituted_items:
        lines.append(f"Substituted ({len(cart.substituted_items)}):")
        for sub in cart.substituted_items:
            orig = sub.original_item.ingredient
            alt = sub.selected.product.name if sub.selected else "(no substitute found)"
            lines.append(f"  {orig} -> {alt}")
        lines.append("")

    lines.append(f"Subtotal:  ${cart.subtotal:.2f}")
    lines.append(f"Fulfillment: {cart.recommended_fulfillment.value}")
    lines.append(f"Est. Total: ${cart.estimated_total:.2f}")

    return "\n".join(lines)


def _handle_order(args: argparse.Namespace) -> int:
    """Handle the ``order`` subcommand.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    cfg = _load_config_safe()
    if cfg is None:
        print(
            "Error: Missing configuration. "
            "Copy .env.example to .env and fill in your keys.",
            file=sys.stderr,
        )
        return 1

    client = _make_anthropic_client(cfg.anthropic_api_key)

    try:
        pipeline = SafewayPipeline(cfg, cfg.database_path, client)
    except SafewayPipelineError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    items = _parse_order_items(args, cfg)
    if items is None:
        return 1

    dry_run: bool = getattr(args, "dry_run", False)

    try:
        if dry_run:
            cart = pipeline.build_cart_only(items)
            print(_format_cart_summary(cart))
            return 0

        result = pipeline.run(items)
        if result.success and result.confirmation:
            print(f"Order submitted! ID: {result.confirmation.order_id}")
            print(
                f"Status: {result.confirmation.status} "
                f"({result.confirmation.item_count} items, "
                f"${result.confirmation.total:.2f})"
            )
            if result.items_restocked:
                print(f"Restocked {result.items_restocked} inventory items.")
            return 0

        print(f"Order failed: {result.error_message}", file=sys.stderr)
        return 1
    except SafewayPipelineError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        pipeline.close()


def _parse_order_items(
    args: argparse.Namespace,
    cfg: Config,
) -> list[ShoppingListItem] | None:
    """Parse order items from CLI arguments.

    Args:
        args: Parsed arguments with items or meals.
        cfg: Application configuration.

    Returns:
        List of ShoppingListItem or None on error.
    """
    items_str: str | None = getattr(args, "items", None)
    meals_str: str | None = getattr(args, "meals", None)

    if items_str:
        return _items_from_string(items_str)

    if meals_str:
        return _items_from_meals(meals_str, cfg)

    print(
        "Error: Provide --items or --meals.",
        file=sys.stderr,
    )
    return None


def _items_from_string(items_str: str) -> list[ShoppingListItem]:
    """Convert comma-separated item names to ShoppingListItem list.

    Args:
        items_str: Comma-separated ingredient names.

    Returns:
        List of ShoppingListItem with defaults.
    """
    names = [n.strip() for n in items_str.split(",") if n.strip()]
    return [
        ShoppingListItem(
            ingredient=name.lower(),
            quantity=1.0,
            unit=Unit.EACH,
            category=IngredientCategory.OTHER,
            search_term=name.lower(),
            from_meals=["manual"],
        )
        for name in names
    ]


def _items_from_meals(meals_str: str, cfg: Config) -> list[ShoppingListItem]:
    """Parse meals and consolidate into shopping list items.

    Args:
        meals_str: Comma-separated meal names.
        cfg: Application config.

    Returns:
        Consolidated shopping list items.
    """
    client = _make_anthropic_client(cfg.anthropic_api_key)
    store = RecipeStore(cfg.database_path)
    parser = MealParser(store, anthropic_client=client, config=cfg)
    consolidator = Consolidator(anthropic_client=client, config=cfg)

    meal_names = [m.strip() for m in meals_str.split(",") if m.strip()]
    parsed_meals = parser.parse_meals(meal_names)

    pantry_mgr = PantryManager(cfg.database_path, anthropic_client=client)
    restock_queue = pantry_mgr.get_restock_queue()
    pantry_staple_names = store.get_pantry_staple_names()

    return consolidator.consolidate(parsed_meals, restock_queue, pantry_staple_names)


def _handle_bot() -> int:
    """Handle the ``bot`` subcommand.

    Loads configuration and starts the Discord bot. Requires
    DISCORD_BOT_TOKEN in the environment.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    cfg = _load_config_safe()
    if cfg is None:
        return 1

    from grocery_butler.bot import run_bot

    try:
        run_bot(cfg)
    except Exception:
        logger.exception("Discord bot exited with an error")
        return 1
    return 0


# ------------------------------------------------------------------
# Argument parser
# ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser with all subcommands.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        prog="grocery-butler",
        description="Grocery Butler: meal planning and shopping list CLI.",
    )
    subparsers = parser.add_subparsers(dest="command")

    _add_plan_parser(subparsers)
    _add_order_parser(subparsers)
    _add_stock_parser(subparsers)
    _add_restock_parser(subparsers)
    _add_recipes_parser(subparsers)
    _add_pantry_parser(subparsers)
    _add_bot_parser(subparsers)

    return parser


def _add_order_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Add the ``order`` subcommand parser.

    Args:
        subparsers: Subparsers action from the main parser.
    """
    order_parser = subparsers.add_parser(
        "order",
        help="Build and submit a Safeway grocery order.",
    )
    order_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Build cart and show summary without submitting.",
    )
    order_parser.add_argument(
        "--items",
        default=None,
        help="Comma-separated list of items (e.g. 'milk, eggs, bread').",
    )
    order_parser.add_argument(
        "--meals",
        default=None,
        help="Comma-separated list of meals to plan and order.",
    )


def _add_bot_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Add the ``bot`` subcommand parser.

    Args:
        subparsers: Subparsers action from the main parser.
    """
    subparsers.add_parser(
        "bot",
        help="Start the Discord bot.",
    )


def _add_plan_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Add the ``plan`` subcommand parser.

    Args:
        subparsers: Subparsers action from the main parser.
    """
    plan_parser = subparsers.add_parser(
        "plan",
        help="Generate a shopping list from meals.",
    )
    plan_parser.add_argument(
        "meals",
        help="Comma-separated list of meal names.",
    )
    plan_parser.add_argument(
        "--servings",
        type=int,
        default=None,
        help="Number of servings (default from config).",
    )
    plan_parser.add_argument(
        "--save",
        action="store_true",
        help="Save unknown recipes to the recipe store.",
    )


def _add_stock_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Add the ``stock`` subcommand parser.

    Args:
        subparsers: Subparsers action from the main parser.
    """
    stock_parser = subparsers.add_parser(
        "stock",
        help="Manage household inventory.",
    )
    stock_parser.add_argument(
        "action",
        nargs="?",
        default=None,
        choices=["out", "low", "good", "add"],
        help="Action: out, low, good, or add.",
    )
    stock_parser.add_argument(
        "item_name",
        nargs="?",
        default="",
        help="Ingredient name.",
    )
    stock_parser.add_argument(
        "category",
        nargs="?",
        default="other",
        help="Category (for 'add' action only).",
    )
    stock_parser.add_argument(
        "--quantity",
        type=float,
        default=None,
        help="Set current quantity (e.g. 2.0).",
    )
    stock_parser.add_argument(
        "--unit",
        default=None,
        help="Unit for quantity (e.g. gal, lb).",
    )


def _add_restock_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Add the ``restock`` subcommand parser.

    Args:
        subparsers: Subparsers action from the main parser.
    """
    restock_parser = subparsers.add_parser(
        "restock",
        help="View or clear the restock queue.",
    )
    restock_parser.add_argument(
        "action",
        nargs="?",
        default=None,
        choices=["clear"],
        help="Action: clear.",
    )


def _add_recipes_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Add the ``recipes`` subcommand parser.

    Args:
        subparsers: Subparsers action from the main parser.
    """
    recipes_parser = subparsers.add_parser(
        "recipes",
        help="Manage saved recipes.",
    )
    recipes_parser.add_argument(
        "action",
        nargs="?",
        default=None,
        choices=["show", "forget"],
        help="Action: show or forget.",
    )
    recipes_parser.add_argument(
        "recipe_name",
        nargs="?",
        default="",
        help="Recipe name.",
    )


def _add_pantry_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Add the ``pantry`` subcommand parser.

    Args:
        subparsers: Subparsers action from the main parser.
    """
    pantry_parser = subparsers.add_parser(
        "pantry",
        help="Manage pantry staples.",
    )
    pantry_parser.add_argument(
        "action",
        nargs="?",
        default=None,
        choices=["add", "remove"],
        help="Action: add or remove.",
    )
    pantry_parser.add_argument(
        "ingredient_name",
        nargs="?",
        default="",
        help="Ingredient name.",
    )
    pantry_parser.add_argument(
        "category",
        nargs="?",
        default="pantry_dry",
        help="Category (for 'add' action only).",
    )


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """Run the CLI application.

    Args:
        argv: Optional argument list (defaults to sys.argv[1:]).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    exit_code = _dispatch(args)
    sys.exit(exit_code)


def _dispatch(args: argparse.Namespace) -> int:
    """Dispatch a parsed command to the appropriate handler.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code from the handler.
    """
    command: str = args.command
    if command == "plan":
        return _handle_plan(args)
    if command == "order":
        return _handle_order(args)
    if command == "stock":
        return _handle_stock(args)
    if command == "restock":
        return _handle_restock(args)
    if command == "recipes":
        return _handle_recipes(args)
    if command == "pantry":
        return _handle_pantry(args)
    if command == "bot":
        return _handle_bot()
    return 1  # pragma: no cover
