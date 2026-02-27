"""Discord bot for Grocery Butler with slash commands and NL inventory.

Provides a Discord interface for managing household inventory, planning
meals, and generating shopping lists. Runs as a separate process sharing
the SQLite database with the Flask app via WAL mode.

Uses Discord's native permission system: slash commands require
``manage_guild`` by default, which server admins can override per-role
via Server Settings > Integrations > Manage.

Security note: This is intentionally a multi-user model. Any guild member
granted ``manage_guild`` (or access via the Integrations UI) can use the
bot. Server admins are responsible for configuring role-based access.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from grocery_butler.config import Config
    from grocery_butler.models import (
        BrandPreference,
        CartSummary,
        InventoryItem,
        ParsedMeal,
        ShoppingListItem,
    )
    from grocery_butler.order_service import OrderResult
    from grocery_butler.safeway_pipeline import SafewayPipeline

logger = logging.getLogger(__name__)

# Status emoji mapping for inventory display
_STATUS_EMOJI: dict[str, str] = {
    "on_hand": "\u2705",
    "low": "\u26a0\ufe0f",
    "out": "\u274c",
}

_MAX_MESSAGE_LENGTH = 1900


def _truncate(text: str, limit: int = _MAX_MESSAGE_LENGTH) -> str:
    """Truncate text to fit within Discord message limits.

    Args:
        text: The text to truncate.
        limit: Maximum character length.

    Returns:
        Truncated text with ellipsis if needed.
    """
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _format_shopping_list(items: list[ShoppingListItem]) -> str:
    """Format a shopping list grouped by category as a code block.

    Args:
        items: List of consolidated shopping list items.

    Returns:
        Formatted shopping list string ready for Discord.
    """
    if not items:
        return "Shopping list is empty!"

    by_category: dict[str, list[ShoppingListItem]] = {}
    for item in items:
        cat = str(item.category).replace("_", " ").title()
        by_category.setdefault(cat, []).append(item)

    lines: list[str] = []
    for category, cat_items in sorted(by_category.items()):
        lines.append(f"\n== {category} ==")
        for item in cat_items:
            qty_str = (
                f"{item.quantity:g} {item.unit}" if item.unit else f"{item.quantity:g}"
            )
            meals_str = ", ".join(item.from_meals) if item.from_meals else ""
            line = f"  {item.ingredient}: {qty_str}"
            if meals_str:
                line += f"  ({meals_str})"
            lines.append(line)

    return _truncate("```\n" + "\n".join(lines) + "\n```")


def _format_inventory(items: list[InventoryItem]) -> str:
    """Format inventory list with status emoji.

    Args:
        items: List of inventory items.

    Returns:
        Formatted inventory string.
    """
    if not items:
        return "No items in inventory."

    lines: list[str] = []
    for item in items:
        emoji = _STATUS_EMOJI.get(item.status.value, "")
        cat_str = f" [{item.category.value}]" if item.category else ""
        qty_str = ""
        if item.current_quantity is not None and item.current_unit is not None:
            qty_str = f" — {item.current_quantity:g} {item.current_unit}"
        lines.append(f"{emoji} **{item.display_name}**{cat_str}{qty_str}")
    return _truncate("\n".join(lines))


def _format_pantry_list(staples: list[dict[str, object]]) -> str:
    """Format pantry staples list for display.

    Args:
        staples: List of pantry staple dicts.

    Returns:
        Formatted pantry list string.
    """
    if not staples:
        return "No pantry staples configured."

    lines: list[str] = []
    for staple in staples:
        name = str(staple.get("display_name", staple.get("ingredient", "")))
        cat = str(staple.get("category", ""))
        cat_str = f" [{cat}]" if cat else ""
        lines.append(f"- {name}{cat_str}")
    return _truncate("\n".join(lines))


def _format_restock_queue(items: list[InventoryItem]) -> str:
    """Format the restock queue for display.

    Args:
        items: List of inventory items needing restocking.

    Returns:
        Formatted restock queue string.
    """
    if not items:
        return "Restock queue is empty!"

    lines: list[str] = []
    for item in items:
        emoji = _STATUS_EMOJI.get(item.status.value, "")
        qty_str = ""
        if item.current_quantity is not None and item.current_unit is not None:
            qty_str = f" ({item.current_quantity:g} {item.current_unit})"
        elif item.default_quantity is not None and item.default_unit is not None:
            qty_str = f" (reorder: {item.default_quantity:g} {item.default_unit})"
        lines.append(f"{emoji} **{item.display_name}**{qty_str}")
    return _truncate("\n".join(lines))


def _format_brands(prefs: list[BrandPreference]) -> str:
    """Format brand preferences list for display.

    Args:
        prefs: List of brand preference models.

    Returns:
        Formatted brand preferences string.
    """
    if not prefs:
        return "No brand preferences set."

    lines: list[str] = []
    for pref in prefs:
        ptype = pref.preference_type.value
        lines.append(f"- **{pref.match_target}**: {pref.brand} ({ptype})")
    return _truncate("\n".join(lines))


def _format_recipes(recipes: list[dict[str, object]]) -> str:
    """Format recipes list for display.

    Args:
        recipes: List of recipe summary dicts.

    Returns:
        Formatted recipes string.
    """
    if not recipes:
        return "No saved recipes."

    lines: list[str] = []
    for recipe in recipes:
        name = str(recipe.get("display_name", ""))
        ordered = recipe.get("times_ordered", 0)
        lines.append(f"- **{name}** (ordered {ordered}x)")
    return _truncate("\n".join(lines))


def _format_recipe_detail(meal: ParsedMeal) -> str:
    """Format a single recipe's detail for display.

    Args:
        meal: The parsed meal to format.

    Returns:
        Formatted recipe detail string.
    """
    lines: list[str] = [
        f"**{meal.name}** ({meal.servings} servings)",
        "",
        "**Purchase Items:**",
    ]
    if meal.purchase_items:
        for item in meal.purchase_items:
            lines.append(f"  - {item.quantity:g} {item.unit} {item.ingredient}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("**Pantry Items:**")
    if meal.pantry_items:
        for item in meal.pantry_items:
            lines.append(f"  - {item.quantity:g} {item.unit} {item.ingredient}")
    else:
        lines.append("  (none)")

    return _truncate("\n".join(lines))


def _format_preferences(prefs: dict[str, str]) -> str:
    """Format preferences dict for display.

    Args:
        prefs: Dict of preference key-value pairs.

    Returns:
        Formatted preferences string.
    """
    if not prefs:
        return "No preferences set."

    lines: list[str] = []
    for key, value in sorted(prefs.items()):
        lines.append(f"- **{key}**: {value}")
    return _truncate("\n".join(lines))


class _OrderConfirmView(discord.ui.View):
    """Discord UI view with Confirm / Cancel buttons for order submission.

    Attributes:
        pipeline: The SafewayPipeline instance to use for submission.
        items: Shopping list items to order.
    """

    def __init__(
        self,
        pipeline: SafewayPipeline,
        items: list[ShoppingListItem],
    ) -> None:
        """Initialize the confirmation view.

        Args:
            pipeline: SafewayPipeline instance.
            items: ShoppingListItem list to submit.
        """
        super().__init__(timeout=120)
        self._pipeline = pipeline
        self._items = items

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[_OrderConfirmView],
    ) -> None:
        """Handle confirm button press.

        Args:
            interaction: Discord interaction context.
            button: The button that was pressed.
        """
        await interaction.response.defer()
        try:
            order_result = await asyncio.to_thread(self._pipeline.run, self._items)
            msg = _format_order_result(order_result)
            await interaction.followup.send(msg)
        except Exception:
            logger.exception("Order submission failed")
            await interaction.followup.send("Order submission failed.")
        finally:
            await asyncio.to_thread(self._pipeline.close)
            self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[_OrderConfirmView],
    ) -> None:
        """Handle cancel button press.

        Args:
            interaction: Discord interaction context.
            button: The button that was pressed.
        """
        await asyncio.to_thread(self._pipeline.close)
        await interaction.response.send_message("Order cancelled.")
        self.stop()


def _make_bot_anthropic_client(config: Config) -> object | None:
    """Create an Anthropic client from bot config.

    Args:
        config: Application config with API key.

    Returns:
        Anthropic client or None if unavailable.
    """
    try:
        import anthropic

        return anthropic.Anthropic(api_key=config.anthropic_api_key)
    except Exception:
        logger.warning("Anthropic client unavailable; Claude features disabled")
        return None


def _format_cart_summary(cart: CartSummary) -> str:
    """Format a CartSummary for Discord display.

    Args:
        cart: Cart summary to format.

    Returns:
        Formatted cart summary string with code block.
    """
    lines: list[str] = []

    if cart.items:
        lines.append(f"Items ({len(cart.items)}):")
        for ci in cart.items:
            lines.append(
                f"  {ci.quantity_to_order}x {ci.safeway_product.name}"
                f"  ${ci.estimated_cost:.2f}"
            )

    if cart.restock_items:
        lines.append(f"\nRestock ({len(cart.restock_items)}):")
        for ci in cart.restock_items:
            lines.append(
                f"  {ci.quantity_to_order}x {ci.safeway_product.name}"
                f"  ${ci.estimated_cost:.2f}"
            )

    if cart.failed_items:
        lines.append(f"\nFailed ({len(cart.failed_items)}):")
        for item in cart.failed_items:
            lines.append(f"  - {item.ingredient}")

    lines.append(f"\nSubtotal: ${cart.subtotal:.2f}")
    lines.append(f"Fulfillment: {cart.recommended_fulfillment.value}")
    lines.append(f"Est. Total: ${cart.estimated_total:.2f}")

    return _truncate("```\n" + "\n".join(lines) + "\n```")


def _format_order_result(result: OrderResult) -> str:
    """Format an OrderResult for Discord display.

    Args:
        result: Order result to format.

    Returns:
        Formatted order result string.
    """
    if not result.success:
        return f"Order failed: {result.error_message}"

    if result.confirmation is None:
        return "Order submitted but no confirmation received."

    conf = result.confirmation
    lines = [
        f"Order submitted! ID: **{conf.order_id}**",
        f"Status: {conf.status}",
        f"Items: {conf.item_count}",
        f"Total: ${conf.total:.2f}",
        f"Fulfillment: {conf.fulfillment_type.value}",
        f"Est. Time: {conf.estimated_time}",
    ]
    if result.items_restocked:
        lines.append(f"Restocked {result.items_restocked} inventory items.")

    return "\n".join(lines)


def create_bot(config: Config) -> discord.Client:
    """Create and configure the Discord bot with all slash commands.

    Args:
        config: Application configuration.

    Returns:
        Configured Discord client ready to run.
    """
    from grocery_butler.consolidator import Consolidator
    from grocery_butler.meal_parser import MealParser
    from grocery_butler.pantry_manager import PantryManager
    from grocery_butler.recipe_store import RecipeStore

    intents = discord.Intents.default()
    intents.message_content = True

    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    # Store references on the client for access in event handlers
    client.tree = tree  # type: ignore[attr-defined]
    client.config = config  # type: ignore[attr-defined]

    # Initialize backend services
    db_path = config.database_path
    recipe_store = RecipeStore(db_path)
    pantry_manager = PantryManager(db_path)
    meal_parser = MealParser(recipe_store)
    consolidator = Consolidator()

    # ------------------------------------------------------------------
    # /meals command
    # ------------------------------------------------------------------

    @tree.command(name="meals", description="Plan meals and generate a shopping list")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(meals="Comma-separated list of meal names")
    async def meals_command(interaction: discord.Interaction, meals: str) -> None:
        """Run the full meal planning pipeline.

        Args:
            interaction: Discord interaction context.
            meals: Comma-separated meal name string.
        """
        await interaction.response.defer()

        try:
            meal_names = [m.strip() for m in meals.split(",") if m.strip()]
            if not meal_names:
                await interaction.followup.send(
                    "Please provide at least one meal name."
                )
                return

            parsed = await asyncio.to_thread(meal_parser.parse_meals, meal_names)
            staples = await asyncio.to_thread(recipe_store.get_pantry_staple_names)
            restock = await asyncio.to_thread(pantry_manager.get_restock_queue)
            shopping_list = await asyncio.to_thread(
                consolidator.consolidate_simple, parsed, restock, staples
            )

            result = _format_shopping_list(shopping_list)
            meal_summary = ", ".join(meal_names)
            await interaction.followup.send(
                f"Shopping list for: **{meal_summary}**\n{result}"
            )
        except Exception:
            logger.exception("Error in /meals command")
            await interaction.followup.send(
                "Sorry, something went wrong generating the shopping list."
            )

    # ------------------------------------------------------------------
    # /pantry command group
    # ------------------------------------------------------------------

    pantry_group = app_commands.Group(
        name="pantry",
        description="Manage pantry staples",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @pantry_group.command(name="list", description="Show all pantry staples")
    async def pantry_list(interaction: discord.Interaction) -> None:
        """Show all pantry staples.

        Args:
            interaction: Discord interaction context.
        """
        try:
            staples = await asyncio.to_thread(recipe_store.get_pantry_staples)
            result = _format_pantry_list(staples)
            await interaction.response.send_message(result)
        except Exception:
            logger.exception("Error in /pantry list")
            await interaction.response.send_message(
                "Sorry, something went wrong listing pantry staples."
            )

    @pantry_group.command(name="add", description="Add a pantry staple")
    @app_commands.describe(
        ingredient="Ingredient name", category="Category (e.g. pantry_dry, dairy)"
    )
    async def pantry_add(
        interaction: discord.Interaction,
        ingredient: str,
        category: str = "other",
    ) -> None:
        """Add a new pantry staple.

        Args:
            interaction: Discord interaction context.
            ingredient: Ingredient name to add.
            category: Category string for the staple.
        """
        try:
            await asyncio.to_thread(
                recipe_store.add_pantry_staple, ingredient, category
            )
            await interaction.response.send_message(
                f"Added **{ingredient}** to pantry staples."
            )
        except Exception:
            logger.exception("Error in /pantry add")
            await interaction.response.send_message(
                f"Could not add '{ingredient}' -- it may already exist."
            )

    @pantry_group.command(name="remove", description="Remove a pantry staple")
    @app_commands.describe(ingredient="Ingredient name to remove")
    async def pantry_remove(interaction: discord.Interaction, ingredient: str) -> None:
        """Remove a pantry staple.

        Args:
            interaction: Discord interaction context.
            ingredient: Ingredient name to remove.
        """
        try:
            staples = await asyncio.to_thread(recipe_store.get_pantry_staples)
            target = ingredient.strip().lower()
            found_id: int | None = None
            for staple in staples:
                ing_name = str(staple.get("ingredient", ""))
                if ing_name.lower() == target:
                    found_id = int(str(staple.get("id", 0)))
                    break

            if found_id is None or found_id == 0:
                await interaction.response.send_message(
                    f"Pantry staple '{ingredient}' not found."
                )
                return

            await asyncio.to_thread(recipe_store.remove_pantry_staple, found_id)
            await interaction.response.send_message(
                f"Removed **{ingredient}** from pantry staples."
            )
        except Exception:
            logger.exception("Error in /pantry remove")
            await interaction.response.send_message(
                "Sorry, something went wrong removing that staple."
            )

    tree.add_command(pantry_group)

    # ------------------------------------------------------------------
    # /stock command group
    # ------------------------------------------------------------------

    stock_group = app_commands.Group(
        name="stock",
        description="Manage household inventory",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @stock_group.command(name="show", description="Show full inventory")
    async def stock_show(interaction: discord.Interaction) -> None:
        """Show full inventory with status indicators.

        Args:
            interaction: Discord interaction context.
        """
        try:
            items = await asyncio.to_thread(pantry_manager.get_inventory)
            result = _format_inventory(items)
            await interaction.response.send_message(result)
        except Exception:
            logger.exception("Error in /stock show")
            await interaction.response.send_message(
                "Sorry, something went wrong showing inventory."
            )

    @stock_group.command(name="out", description="Mark an item as out of stock")
    @app_commands.describe(item="Item name")
    async def stock_out(interaction: discord.Interaction, item: str) -> None:
        """Mark an item as out of stock.

        Args:
            interaction: Discord interaction context.
            item: Item name to mark as out.
        """
        try:
            from grocery_butler.models import InventoryStatus

            existing = await asyncio.to_thread(pantry_manager.get_item, item)
            if existing is None:
                await interaction.response.send_message(
                    f"Item '{item}' not found. Use `/stock add` to track it first."
                )
                return
            await asyncio.to_thread(
                pantry_manager.update_status, item, InventoryStatus.OUT
            )
            await interaction.response.send_message(
                f"Marked **{item}** as out of stock."
            )
        except Exception:
            logger.exception("Error in /stock out")
            await interaction.response.send_message(
                "Sorry, something went wrong updating inventory."
            )

    @stock_group.command(name="low", description="Mark an item as running low")
    @app_commands.describe(item="Item name")
    async def stock_low(interaction: discord.Interaction, item: str) -> None:
        """Mark an item as running low.

        Args:
            interaction: Discord interaction context.
            item: Item name to mark as low.
        """
        try:
            from grocery_butler.models import InventoryStatus

            existing = await asyncio.to_thread(pantry_manager.get_item, item)
            if existing is None:
                await interaction.response.send_message(
                    f"Item '{item}' not found. Use `/stock add` to track it first."
                )
                return
            await asyncio.to_thread(
                pantry_manager.update_status, item, InventoryStatus.LOW
            )
            await interaction.response.send_message(
                f"Marked **{item}** as running low."
            )
        except Exception:
            logger.exception("Error in /stock low")
            await interaction.response.send_message(
                "Sorry, something went wrong updating inventory."
            )

    @stock_group.command(name="good", description="Mark an item as on hand")
    @app_commands.describe(item="Item name")
    async def stock_good(interaction: discord.Interaction, item: str) -> None:
        """Mark an item as on hand.

        Args:
            interaction: Discord interaction context.
            item: Item name to mark as on_hand.
        """
        try:
            from grocery_butler.models import InventoryStatus

            existing = await asyncio.to_thread(pantry_manager.get_item, item)
            if existing is None:
                await interaction.response.send_message(
                    f"Item '{item}' not found. Use `/stock add` to track it first."
                )
                return
            await asyncio.to_thread(
                pantry_manager.update_status, item, InventoryStatus.ON_HAND
            )
            await interaction.response.send_message(f"Marked **{item}** as on hand.")
        except Exception:
            logger.exception("Error in /stock good")
            await interaction.response.send_message(
                "Sorry, something went wrong updating inventory."
            )

    @stock_group.command(name="add", description="Track a new inventory item")
    @app_commands.describe(item="Item name", category="Category (e.g. produce, dairy)")
    async def stock_add(
        interaction: discord.Interaction,
        item: str,
        category: str = "other",
    ) -> None:
        """Add a new tracked inventory item.

        Args:
            interaction: Discord interaction context.
            item: Item name to track.
            category: Category string.
        """
        try:
            from grocery_butler.models import (
                IngredientCategory,
                InventoryItem,
                InventoryStatus,
            )

            try:
                cat_enum = IngredientCategory(category.lower())
            except ValueError:
                cat_enum = IngredientCategory.OTHER

            inv_item = InventoryItem(
                ingredient=item.lower(),
                display_name=item.strip().title(),
                category=cat_enum,
                status=InventoryStatus.ON_HAND,
            )
            await asyncio.to_thread(pantry_manager.add_item, inv_item)
            await interaction.response.send_message(
                f"Now tracking **{item}** in inventory."
            )
        except Exception:
            logger.exception("Error in /stock add")
            await interaction.response.send_message(
                f"Could not add '{item}' -- it may already be tracked."
            )

    tree.add_command(stock_group)

    # ------------------------------------------------------------------
    # /restock command group
    # ------------------------------------------------------------------

    restock_group = app_commands.Group(
        name="restock",
        description="View and manage the restock queue",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @restock_group.command(name="show", description="Show the restock queue")
    async def restock_show(interaction: discord.Interaction) -> None:
        """Show the restock queue.

        Args:
            interaction: Discord interaction context.
        """
        try:
            items = await asyncio.to_thread(pantry_manager.get_restock_queue)
            result = _format_restock_queue(items)
            await interaction.response.send_message(result)
        except Exception:
            logger.exception("Error in /restock show")
            await interaction.response.send_message(
                "Sorry, something went wrong showing the restock queue."
            )

    @restock_group.command(name="clear", description="Clear the restock queue")
    async def restock_clear(interaction: discord.Interaction) -> None:
        """Clear the restock queue by marking all items as on_hand.

        Args:
            interaction: Discord interaction context.
        """
        try:
            count = await asyncio.to_thread(pantry_manager.clear_restock_queue)
            await interaction.response.send_message(
                f"Cleared restock queue ({count} items restocked)."
            )
        except Exception:
            logger.exception("Error in /restock clear")
            await interaction.response.send_message(
                "Sorry, something went wrong clearing the restock queue."
            )

    tree.add_command(restock_group)

    # ------------------------------------------------------------------
    # /brands command group
    # ------------------------------------------------------------------

    brands_group = app_commands.Group(
        name="brands",
        description="Manage brand preferences",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @brands_group.command(name="show", description="Show all brand preferences")
    async def brands_show(interaction: discord.Interaction) -> None:
        """Show all brand preferences.

        Args:
            interaction: Discord interaction context.
        """
        try:
            prefs = await asyncio.to_thread(recipe_store.get_brand_preferences)
            result = _format_brands(prefs)
            await interaction.response.send_message(result)
        except Exception:
            logger.exception("Error in /brands show")
            await interaction.response.send_message(
                "Sorry, something went wrong showing brand preferences."
            )

    @brands_group.command(name="set", description="Set a brand preference")
    @app_commands.describe(
        target="Ingredient or category name", brand="Preferred brand"
    )
    async def brands_set(
        interaction: discord.Interaction, target: str, brand: str
    ) -> None:
        """Set a preferred brand for an ingredient or category.

        Args:
            interaction: Discord interaction context.
            target: Ingredient or category name.
            brand: The preferred brand name.
        """
        try:
            from grocery_butler.models import (
                BrandMatchType,
                BrandPreference,
                BrandPreferenceType,
            )

            pref = BrandPreference(
                match_target=target.lower(),
                match_type=BrandMatchType.INGREDIENT,
                brand=brand,
                preference_type=BrandPreferenceType.PREFERRED,
            )
            await asyncio.to_thread(recipe_store.add_brand_preference, pref)
            await interaction.response.send_message(
                f"Set preferred brand for **{target}**: {brand}"
            )
        except Exception:
            logger.exception("Error in /brands set")
            await interaction.response.send_message(
                "Sorry, something went wrong setting brand preference."
            )

    @brands_group.command(name="avoid", description="Add a brand to the avoid list")
    @app_commands.describe(brand="Brand name to avoid")
    async def brands_avoid(interaction: discord.Interaction, brand: str) -> None:
        """Add a brand to the avoid list.

        Args:
            interaction: Discord interaction context.
            brand: Brand name to avoid.
        """
        try:
            from grocery_butler.models import (
                BrandMatchType,
                BrandPreference,
                BrandPreferenceType,
            )

            pref = BrandPreference(
                match_target="*",
                match_type=BrandMatchType.CATEGORY,
                brand=brand,
                preference_type=BrandPreferenceType.AVOID,
            )
            await asyncio.to_thread(recipe_store.add_brand_preference, pref)
            await interaction.response.send_message(f"Added **{brand}** to avoid list.")
        except Exception:
            logger.exception("Error in /brands avoid")
            await interaction.response.send_message(
                "Sorry, something went wrong adding brand to avoid list."
            )

    @brands_group.command(name="clear", description="Remove a brand preference")
    @app_commands.describe(target="Ingredient or category name to clear")
    async def brands_clear(interaction: discord.Interaction, target: str) -> None:
        """Remove brand preferences for a target.

        Args:
            interaction: Discord interaction context.
            target: The target to clear preferences for.
        """
        try:
            prefs = await asyncio.to_thread(recipe_store.get_brand_preferences)
            removed = 0
            for idx, pref in enumerate(prefs):
                if pref.match_target.lower() == target.lower():
                    # Brand preference IDs start at 1 and are sequential
                    # Re-fetch to get current IDs
                    await asyncio.to_thread(
                        recipe_store.remove_brand_preference, idx + 1
                    )
                    removed += 1

            if removed > 0:
                await interaction.response.send_message(
                    f"Cleared brand preferences for **{target}**."
                )
            else:
                await interaction.response.send_message(
                    f"No brand preferences found for '{target}'."
                )
        except Exception:
            logger.exception("Error in /brands clear")
            await interaction.response.send_message(
                "Sorry, something went wrong clearing brand preferences."
            )

    tree.add_command(brands_group)

    # ------------------------------------------------------------------
    # /recipes command group
    # ------------------------------------------------------------------

    recipes_group = app_commands.Group(
        name="recipes",
        description="Manage saved recipes",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @recipes_group.command(name="list", description="List all saved recipes")
    async def recipes_list(interaction: discord.Interaction) -> None:
        """List all saved recipes.

        Args:
            interaction: Discord interaction context.
        """
        try:
            recipes = await asyncio.to_thread(recipe_store.list_recipes)
            result = _format_recipes(recipes)
            await interaction.response.send_message(result)
        except Exception:
            logger.exception("Error in /recipes list")
            await interaction.response.send_message(
                "Sorry, something went wrong listing recipes."
            )

    @recipes_group.command(name="show", description="Show recipe details")
    @app_commands.describe(name="Recipe name")
    async def recipes_show(interaction: discord.Interaction, name: str) -> None:
        """Show detailed recipe information.

        Args:
            interaction: Discord interaction context.
            name: Recipe name to look up.
        """
        try:
            meal = await asyncio.to_thread(recipe_store.find_recipe, name)
            if meal is None:
                await interaction.response.send_message(f"Recipe '{name}' not found.")
                return
            result = _format_recipe_detail(meal)
            await interaction.response.send_message(result)
        except Exception:
            logger.exception("Error in /recipes show")
            await interaction.response.send_message(
                "Sorry, something went wrong showing that recipe."
            )

    @recipes_group.command(name="forget", description="Delete a saved recipe")
    @app_commands.describe(name="Recipe name to delete")
    async def recipes_forget(interaction: discord.Interaction, name: str) -> None:
        """Delete a saved recipe.

        Args:
            interaction: Discord interaction context.
            name: Recipe name to delete.
        """
        try:
            recipes = await asyncio.to_thread(recipe_store.list_recipes)
            from grocery_butler.recipe_store import normalize_recipe_name

            normalized = normalize_recipe_name(name)
            found_id: int | None = None
            for recipe in recipes:
                if str(recipe.get("name", "")) == normalized:
                    found_id = int(str(recipe.get("id", 0)))
                    break

            if found_id is None or found_id == 0:
                await interaction.response.send_message(f"Recipe '{name}' not found.")
                return

            await asyncio.to_thread(recipe_store.delete_recipe, found_id)
            await interaction.response.send_message(f"Deleted recipe **{name}**.")
        except Exception:
            logger.exception("Error in /recipes forget")
            await interaction.response.send_message(
                "Sorry, something went wrong deleting that recipe."
            )

    tree.add_command(recipes_group)

    # ------------------------------------------------------------------
    # /preferences command group
    # ------------------------------------------------------------------

    prefs_group = app_commands.Group(
        name="preferences",
        description="Manage user preferences",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @prefs_group.command(name="show", description="Show current preferences")
    async def prefs_show(interaction: discord.Interaction) -> None:
        """Show all current preferences.

        Args:
            interaction: Discord interaction context.
        """
        try:
            prefs = await asyncio.to_thread(recipe_store.get_all_preferences)
            result = _format_preferences(prefs)
            await interaction.response.send_message(result)
        except Exception:
            logger.exception("Error in /preferences show")
            await interaction.response.send_message(
                "Sorry, something went wrong showing preferences."
            )

    @prefs_group.command(name="set", description="Update a preference")
    @app_commands.describe(key="Preference key", value="Preference value")
    async def prefs_set(interaction: discord.Interaction, key: str, value: str) -> None:
        """Set a preference value.

        Args:
            interaction: Discord interaction context.
            key: Preference key.
            value: Preference value.
        """
        try:
            await asyncio.to_thread(recipe_store.set_preference, key, value)
            await interaction.response.send_message(f"Set **{key}** = {value}")
        except Exception:
            logger.exception("Error in /preferences set")
            await interaction.response.send_message(
                "Sorry, something went wrong updating that preference."
            )

    tree.add_command(prefs_group)

    # ------------------------------------------------------------------
    # /order command group
    # ------------------------------------------------------------------

    order_group = app_commands.Group(
        name="order",
        description="Safeway grocery ordering",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @order_group.command(name="review", description="Build cart and show summary")
    @app_commands.describe(items="Comma-separated item names (e.g. milk, eggs)")
    async def order_review(interaction: discord.Interaction, items: str) -> None:
        """Build a Safeway cart and show summary without ordering.

        Args:
            interaction: Discord interaction context.
            items: Comma-separated item names.
        """
        await interaction.response.defer()

        try:
            from grocery_butler.models import IngredientCategory, ShoppingListItem, Unit
            from grocery_butler.safeway_pipeline import (
                SafewayPipeline,
                SafewayPipelineError,
            )

            names = [n.strip() for n in items.split(",") if n.strip()]
            if not names:
                await interaction.followup.send("Please provide item names.")
                return

            shopping_items = [
                ShoppingListItem(
                    ingredient=n.lower(),
                    quantity=1.0,
                    unit=Unit.EACH,
                    category=IngredientCategory.OTHER,
                    search_term=n.lower(),
                    from_meals=["manual"],
                )
                for n in names
            ]

            anthropic_client = _make_bot_anthropic_client(config)
            pipeline = SafewayPipeline(config, config.database_path, anthropic_client)
            try:
                cart = await asyncio.to_thread(pipeline.build_cart_only, shopping_items)
                result = _format_cart_summary(cart)
                await interaction.followup.send(f"Cart preview:\n{result}")
            finally:
                await asyncio.to_thread(pipeline.close)

        except SafewayPipelineError as exc:
            await interaction.followup.send(f"Pipeline error: {exc}")
        except Exception:
            logger.exception("Error in /order review")
            await interaction.followup.send(
                "Sorry, something went wrong building the cart."
            )

    @order_group.command(name="submit", description="Build and submit a Safeway order")
    @app_commands.describe(items="Comma-separated item names (e.g. milk, eggs)")
    async def order_submit(interaction: discord.Interaction, items: str) -> None:
        """Build cart, show preview, and submit order after confirmation.

        Args:
            interaction: Discord interaction context.
            items: Comma-separated item names.
        """
        await interaction.response.defer()

        try:
            from grocery_butler.models import IngredientCategory, ShoppingListItem, Unit
            from grocery_butler.safeway_pipeline import (
                SafewayPipeline,
                SafewayPipelineError,
            )

            names = [n.strip() for n in items.split(",") if n.strip()]
            if not names:
                await interaction.followup.send("Please provide item names.")
                return

            shopping_items = [
                ShoppingListItem(
                    ingredient=n.lower(),
                    quantity=1.0,
                    unit=Unit.EACH,
                    category=IngredientCategory.OTHER,
                    search_term=n.lower(),
                    from_meals=["manual"],
                )
                for n in names
            ]

            anthropic_client = _make_bot_anthropic_client(config)
            pipeline = SafewayPipeline(config, config.database_path, anthropic_client)
            try:
                cart = await asyncio.to_thread(pipeline.build_cart_only, shopping_items)
                preview = _format_cart_summary(cart)
                view = _OrderConfirmView(pipeline, shopping_items)
                await interaction.followup.send(
                    f"{preview}\n\nSubmit this order?", view=view
                )
            except Exception:
                await asyncio.to_thread(pipeline.close)
                raise

        except SafewayPipelineError as exc:
            await interaction.followup.send(f"Pipeline error: {exc}")
        except Exception:
            logger.exception("Error in /order submit")
            await interaction.followup.send(
                "Sorry, something went wrong submitting the order."
            )

    tree.add_command(order_group)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    @client.event
    async def on_ready() -> None:
        """Handle bot startup: sync commands and log ready state."""
        if client.user is not None:
            logger.info("Bot logged in as %s", client.user)
        await tree.sync()
        logger.info("Slash commands synced")

    @client.event
    async def on_message(message: discord.Message) -> None:
        """Handle non-command messages for natural language inventory updates.

        Args:
            message: The incoming Discord message.
        """
        # Ignore bot's own messages
        if message.author == client.user:
            return

        # Ignore DMs (guild-only, consistent with slash commands)
        if message.guild is None:
            return

        # Only process messages from guild members with manage_guild permission.
        # message.author can be discord.User for webhooks/system messages;
        # permissions_for() requires discord.Member, so filter those out.
        member = message.author
        if not isinstance(member, discord.Member):
            return
        perms = message.channel.permissions_for(member)
        if not perms.manage_guild:
            return

        # Ignore messages that look like commands
        if message.content.startswith("/"):
            return

        content = message.content.strip()
        if not content:
            return

        try:
            updates = await asyncio.to_thread(
                pantry_manager.parse_inventory_intent, content
            )

            if not updates:
                return

            # Process high-confidence updates
            high_conf = [u for u in updates if u.confidence >= 0.8]
            low_conf = [u for u in updates if u.confidence < 0.8]

            for update in high_conf:
                await asyncio.to_thread(
                    pantry_manager.update_status,
                    update.ingredient,
                    update.new_status,
                )

            if high_conf:
                lines = []
                for update in high_conf:
                    emoji = _STATUS_EMOJI.get(update.new_status.value, "")
                    lines.append(
                        f"{emoji} **{update.ingredient}** -> {update.new_status.value}"
                    )
                await message.reply("Updated inventory:\n" + "\n".join(lines))

            if low_conf:
                lines = []
                for update in low_conf:
                    lines.append(
                        f"- {update.ingredient} -> {update.new_status.value}"
                        f" (confidence: {update.confidence:.0%})"
                    )
                await message.reply(
                    "I'm not sure about these updates. "
                    "Could you clarify?\n" + "\n".join(lines)
                )
        except Exception:
            logger.exception("Error processing natural language message")

    return client


def run_bot(config: Config) -> None:
    """Create and run the Discord bot.

    Args:
        config: Application configuration.

    Raises:
        grocery_butler.config.ConfigError: If Discord configuration is missing.
    """
    from grocery_butler.config import ConfigError

    if not config.discord_bot_token:
        raise ConfigError("DISCORD_BOT_TOKEN is required to run the bot.")

    client = create_bot(config)
    client.run(config.discord_bot_token, log_handler=None)


if __name__ == "__main__":  # pragma: no cover
    from grocery_butler.config import load_config

    logging.basicConfig(level=logging.INFO)
    run_bot(load_config())
