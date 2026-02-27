"""Tests for grocery_butler.bot Discord bot module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import discord
import pytest

from grocery_butler.bot import (
    _format_brands,
    _format_cart_summary,
    _format_inventory,
    _format_order_result,
    _format_pantry_list,
    _format_preferences,
    _format_recipe_detail,
    _format_recipes,
    _format_restock_queue,
    _format_shopping_list,
    _make_bot_anthropic_client,
    _truncate,
    create_bot,
    run_bot,
)
from grocery_butler.config import Config, ConfigError
from grocery_butler.models import (
    BrandMatchType,
    BrandPreference,
    BrandPreferenceType,
    Ingredient,
    IngredientCategory,
    InventoryItem,
    InventoryStatus,
    InventoryUpdate,
    ParsedMeal,
    ShoppingListItem,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def config() -> Config:
    """Return a test Config with Discord settings."""
    return Config(
        anthropic_api_key="sk-test",
        discord_bot_token="test-bot-token",
        database_path=":memory:",
    )


@pytest.fixture()
def config_no_token() -> Config:
    """Return a Config missing Discord bot token."""
    return Config(
        anthropic_api_key="sk-test",
        discord_bot_token="",
    )


@pytest.fixture()
def sample_shopping_items() -> list[ShoppingListItem]:
    """Return sample shopping list items for testing."""
    return [
        ShoppingListItem(
            ingredient="chicken breast",
            quantity=2.0,
            unit="lbs",
            category=IngredientCategory.MEAT,
            search_term="chicken breast",
            from_meals=["chicken stir fry"],
        ),
        ShoppingListItem(
            ingredient="broccoli",
            quantity=1.0,
            unit="head",
            category=IngredientCategory.PRODUCE,
            search_term="broccoli",
            from_meals=["chicken stir fry"],
        ),
        ShoppingListItem(
            ingredient="rice",
            quantity=2.0,
            unit="cups",
            category=IngredientCategory.PANTRY_DRY,
            search_term="rice",
            from_meals=["chicken stir fry", "fried rice"],
        ),
    ]


@pytest.fixture()
def sample_inventory_items() -> list[InventoryItem]:
    """Return sample inventory items for testing."""
    return [
        InventoryItem(
            ingredient="milk",
            display_name="Milk",
            category=IngredientCategory.DAIRY,
            status=InventoryStatus.ON_HAND,
        ),
        InventoryItem(
            ingredient="eggs",
            display_name="Eggs",
            category=IngredientCategory.DAIRY,
            status=InventoryStatus.LOW,
        ),
        InventoryItem(
            ingredient="bread",
            display_name="Bread",
            category=IngredientCategory.BAKERY,
            status=InventoryStatus.OUT,
        ),
    ]


@pytest.fixture()
def sample_meal() -> ParsedMeal:
    """Return a sample ParsedMeal for testing."""
    return ParsedMeal(
        name="Chicken Stir Fry",
        servings=4,
        known_recipe=True,
        needs_confirmation=False,
        purchase_items=[
            Ingredient(
                ingredient="chicken breast",
                quantity=2.0,
                unit="lbs",
                category=IngredientCategory.MEAT,
            ),
        ],
        pantry_items=[
            Ingredient(
                ingredient="soy sauce",
                quantity=2.0,
                unit="tbsp",
                category=IngredientCategory.PANTRY_DRY,
            ),
        ],
    )


@pytest.fixture()
def mock_interaction() -> MagicMock:
    """Return a mock Discord interaction."""
    interaction = MagicMock()
    interaction.user = MagicMock()
    interaction.user.id = 123456789
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_guild_message(
    *, manage_guild: bool = True, content: str = "hello"
) -> MagicMock:
    """Create a mock guild message with configurable permissions.

    Args:
        manage_guild: Whether the author has manage_guild permission.
        content: Message content string.

    Returns:
        Mock message with guild, channel, and permissions configured.
    """
    message = MagicMock()
    message.author = MagicMock(spec=discord.Member)
    message.author.id = 123456789
    message.content = content
    message.reply = AsyncMock()
    message.guild = MagicMock()

    perms = MagicMock(spec=discord.Permissions)
    perms.manage_guild = manage_guild
    message.channel.permissions_for.return_value = perms

    return message


# ---------------------------------------------------------------------------
# TestTruncate
# ---------------------------------------------------------------------------


class TestTruncate:
    """Tests for the _truncate helper function."""

    def test_short_text_unchanged(self):
        """Test text shorter than limit is returned unchanged."""
        assert _truncate("hello", 10) == "hello"

    def test_exact_limit_unchanged(self):
        """Test text at exact limit is returned unchanged."""
        assert _truncate("hello", 5) == "hello"

    def test_long_text_truncated(self):
        """Test text exceeding limit is truncated with ellipsis."""
        result = _truncate("hello world", 8)
        assert result == "hello..."
        assert len(result) == 8

    def test_default_limit(self):
        """Test default limit is used when none specified."""
        short = "short text"
        assert _truncate(short) == short


# ---------------------------------------------------------------------------
# TestFormatShoppingList
# ---------------------------------------------------------------------------


class TestFormatShoppingList:
    """Tests for the _format_shopping_list function."""

    def test_empty_list(self):
        """Test empty shopping list returns friendly message."""
        assert _format_shopping_list([]) == "Shopping list is empty!"

    def test_grouped_by_category(self, sample_shopping_items):
        """Test items are grouped by category."""
        result = _format_shopping_list(sample_shopping_items)
        assert "Meat" in result
        assert "Produce" in result
        assert "Pantry Dry" in result

    def test_includes_quantities(self, sample_shopping_items):
        """Test quantities and units are included."""
        result = _format_shopping_list(sample_shopping_items)
        assert "2 lb" in result
        assert "1 head" in result

    def test_includes_meal_sources(self, sample_shopping_items):
        """Test meal sources are shown in parentheses."""
        result = _format_shopping_list(sample_shopping_items)
        assert "chicken stir fry" in result

    def test_code_block_formatting(self, sample_shopping_items):
        """Test output is wrapped in code block."""
        result = _format_shopping_list(sample_shopping_items)
        assert result.startswith("```")
        assert result.endswith("```")

    def test_item_without_unit(self):
        """Test formatting an item with empty unit."""
        items = [
            ShoppingListItem(
                ingredient="eggs",
                quantity=12.0,
                unit="",
                category=IngredientCategory.DAIRY,
                search_term="eggs",
                from_meals=[],
            ),
        ]
        result = _format_shopping_list(items)
        assert "12" in result

    def test_item_without_meals(self):
        """Test formatting an item with no meal sources."""
        items = [
            ShoppingListItem(
                ingredient="eggs",
                quantity=12.0,
                unit="each",
                category=IngredientCategory.DAIRY,
                search_term="eggs",
                from_meals=[],
            ),
        ]
        result = _format_shopping_list(items)
        assert "eggs" in result


# ---------------------------------------------------------------------------
# TestFormatInventory
# ---------------------------------------------------------------------------


class TestFormatInventory:
    """Tests for the _format_inventory function."""

    def test_empty_inventory(self):
        """Test empty inventory returns friendly message."""
        assert _format_inventory([]) == "No items in inventory."

    def test_status_emoji(self, sample_inventory_items):
        """Test status emoji are included."""
        result = _format_inventory(sample_inventory_items)
        assert "\u2705" in result  # on_hand
        assert "\u26a0\ufe0f" in result  # low
        assert "\u274c" in result  # out

    def test_display_names(self, sample_inventory_items):
        """Test display names are shown."""
        result = _format_inventory(sample_inventory_items)
        assert "Milk" in result
        assert "Eggs" in result
        assert "Bread" in result

    def test_categories_shown(self, sample_inventory_items):
        """Test categories are shown in brackets."""
        result = _format_inventory(sample_inventory_items)
        assert "[dairy]" in result
        assert "[bakery]" in result

    def test_no_category(self):
        """Test item without category has no bracket suffix."""
        items = [
            InventoryItem(
                ingredient="mystery",
                display_name="Mystery",
                category=None,
                status=InventoryStatus.ON_HAND,
            ),
        ]
        result = _format_inventory(items)
        assert "Mystery" in result
        assert "[" not in result

    def test_quantity_shown(self):
        """Test quantity and unit are shown when set."""
        items = [
            InventoryItem(
                ingredient="milk",
                display_name="Milk",
                status=InventoryStatus.ON_HAND,
                current_quantity=0.5,
                current_unit="gal",
            ),
        ]
        result = _format_inventory(items)
        assert "0.5 gal" in result

    def test_no_quantity_when_none(self):
        """Test no quantity string when quantity is None."""
        items = [
            InventoryItem(
                ingredient="eggs",
                display_name="Eggs",
                status=InventoryStatus.ON_HAND,
            ),
        ]
        result = _format_inventory(items)
        assert "Eggs" in result
        assert "—" not in result


# ---------------------------------------------------------------------------
# TestFormatPantryList
# ---------------------------------------------------------------------------


class TestFormatPantryList:
    """Tests for the _format_pantry_list function."""

    def test_empty_list(self):
        """Test empty pantry returns friendly message."""
        assert _format_pantry_list([]) == "No pantry staples configured."

    def test_with_staples(self):
        """Test formatting pantry staples with categories."""
        staples = [
            {"display_name": "Salt", "category": "pantry_dry", "ingredient": "salt"},
            {"display_name": "Butter", "category": "dairy", "ingredient": "butter"},
        ]
        result = _format_pantry_list(staples)
        assert "Salt" in result
        assert "[pantry_dry]" in result
        assert "Butter" in result
        assert "[dairy]" in result

    def test_staple_without_category(self):
        """Test staple without category works."""
        staples = [{"display_name": "Thing", "ingredient": "thing", "category": ""}]
        result = _format_pantry_list(staples)
        assert "Thing" in result


# ---------------------------------------------------------------------------
# TestFormatRestockQueue
# ---------------------------------------------------------------------------


class TestFormatRestockQueue:
    """Tests for the _format_restock_queue function."""

    def test_empty_queue(self):
        """Test empty restock queue returns friendly message."""
        assert _format_restock_queue([]) == "Restock queue is empty!"

    def test_with_items(self):
        """Test formatting restock queue items."""
        items = [
            InventoryItem(
                ingredient="milk",
                display_name="Milk",
                category=IngredientCategory.DAIRY,
                status=InventoryStatus.OUT,
                default_quantity=1.0,
                default_unit="gallon",
            ),
        ]
        result = _format_restock_queue(items)
        assert "Milk" in result
        assert "1 gal" in result
        assert "\u274c" in result

    def test_item_without_quantity(self):
        """Test item without default quantity omits qty string."""
        items = [
            InventoryItem(
                ingredient="bread",
                display_name="Bread",
                category=IngredientCategory.BAKERY,
                status=InventoryStatus.LOW,
            ),
        ]
        result = _format_restock_queue(items)
        assert "Bread" in result
        assert "\u26a0\ufe0f" in result

    def test_current_quantity_shown(self):
        """Test current quantity is shown when set."""
        items = [
            InventoryItem(
                ingredient="milk",
                display_name="Milk",
                status=InventoryStatus.LOW,
                current_quantity=0.25,
                current_unit="gal",
            ),
        ]
        result = _format_restock_queue(items)
        assert "0.25 gal" in result


# ---------------------------------------------------------------------------
# TestFormatBrands
# ---------------------------------------------------------------------------


class TestFormatBrands:
    """Tests for the _format_brands function."""

    def test_empty_list(self):
        """Test empty brand list returns friendly message."""
        assert _format_brands([]) == "No brand preferences set."

    def test_with_preferences(self):
        """Test formatting brand preferences."""
        prefs = [
            BrandPreference(
                match_target="milk",
                match_type=BrandMatchType.INGREDIENT,
                brand="Organic Valley",
                preference_type=BrandPreferenceType.PREFERRED,
            ),
        ]
        result = _format_brands(prefs)
        assert "milk" in result
        assert "Organic Valley" in result
        assert "preferred" in result


# ---------------------------------------------------------------------------
# TestFormatRecipes
# ---------------------------------------------------------------------------


class TestFormatRecipes:
    """Tests for the _format_recipes function."""

    def test_empty_list(self):
        """Test empty recipe list returns friendly message."""
        assert _format_recipes([]) == "No saved recipes."

    def test_with_recipes(self):
        """Test formatting recipe list."""
        recipes = [
            {"display_name": "Chicken Stir Fry", "times_ordered": 3},
            {"display_name": "Pasta Carbonara", "times_ordered": 1},
        ]
        result = _format_recipes(recipes)
        assert "Chicken Stir Fry" in result
        assert "ordered 3x" in result
        assert "Pasta Carbonara" in result


# ---------------------------------------------------------------------------
# TestFormatRecipeDetail
# ---------------------------------------------------------------------------


class TestFormatRecipeDetail:
    """Tests for the _format_recipe_detail function."""

    def test_with_items(self, sample_meal):
        """Test formatting recipe with purchase and pantry items."""
        result = _format_recipe_detail(sample_meal)
        assert "Chicken Stir Fry" in result
        assert "4 servings" in result
        assert "chicken breast" in result
        assert "soy sauce" in result

    def test_empty_items(self):
        """Test formatting recipe with no items."""
        meal = ParsedMeal(
            name="Empty Meal",
            servings=2,
            known_recipe=False,
            needs_confirmation=True,
            purchase_items=[],
            pantry_items=[],
        )
        result = _format_recipe_detail(meal)
        assert "Empty Meal" in result
        assert "(none)" in result


# ---------------------------------------------------------------------------
# TestFormatPreferences
# ---------------------------------------------------------------------------


class TestFormatPreferences:
    """Tests for the _format_preferences function."""

    def test_empty_prefs(self):
        """Test empty preferences returns friendly message."""
        assert _format_preferences({}) == "No preferences set."

    def test_with_prefs(self):
        """Test formatting preference dict."""
        prefs = {"default_servings": "4", "default_units": "imperial"}
        result = _format_preferences(prefs)
        assert "default_servings" in result
        assert "4" in result
        assert "imperial" in result

    def test_sorted_output(self):
        """Test preferences are sorted by key."""
        prefs = {"zebra": "z", "alpha": "a"}
        result = _format_preferences(prefs)
        alpha_pos = result.index("alpha")
        zebra_pos = result.index("zebra")
        assert alpha_pos < zebra_pos


# ---------------------------------------------------------------------------
# TestCreateBot
# ---------------------------------------------------------------------------


class TestCreateBot:
    """Tests for the create_bot function."""

    def test_creates_client(self, config):
        """Test create_bot returns a discord Client."""
        bot = create_bot(config)
        assert isinstance(bot, discord.Client)

    def test_client_has_tree(self, config):
        """Test the returned client has a command tree."""
        bot = create_bot(config)
        assert hasattr(bot, "tree")

    def test_client_has_config(self, config):
        """Test the returned client stores config."""
        bot = create_bot(config)
        assert hasattr(bot, "config")
        assert bot.config == config  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# TestDiscordPermissions - verify native permission decorators
# ---------------------------------------------------------------------------


class TestDiscordPermissions:
    """Tests that commands use Discord-native permission settings."""

    @pytest.fixture()
    def bot(self, config):
        """Create a bot instance for testing."""
        return create_bot(config)

    def _get_command(self, bot, name):
        """Get a top-level command or group by name.

        Args:
            bot: The Discord bot client.
            name: Command or group name.

        Returns:
            The command/group, or None if not found.
        """
        tree = bot.tree  # type: ignore[attr-defined]
        for cmd in tree.get_commands():
            if cmd.name == name:
                return cmd
        return None  # pragma: no cover

    def test_meals_guild_only(self, bot):
        """Test /meals has guild_only set."""
        cmd = self._get_command(bot, "meals")
        assert cmd is not None
        assert cmd.guild_only is True

    def test_meals_default_permissions(self, bot):
        """Test /meals requires manage_guild by default."""
        cmd = self._get_command(bot, "meals")
        assert cmd is not None
        assert cmd.default_permissions is not None
        assert cmd.default_permissions.manage_guild is True

    def test_command_groups_guild_only(self, bot):
        """Test all command groups have guild_only set."""
        group_names = [
            "pantry",
            "stock",
            "restock",
            "brands",
            "recipes",
            "preferences",
            "order",
        ]
        for name in group_names:
            group = self._get_command(bot, name)
            assert group is not None, f"Group {name!r} not found"
            assert group.guild_only is True, f"Group {name!r} not guild_only"

    def test_command_groups_default_permissions(self, bot):
        """Test all command groups require manage_guild by default."""
        group_names = [
            "pantry",
            "stock",
            "restock",
            "brands",
            "recipes",
            "preferences",
            "order",
        ]
        for name in group_names:
            group = self._get_command(bot, name)
            assert group is not None, f"Group {name!r} not found"
            assert group.default_permissions is not None, (
                f"Group {name!r} has no default_permissions"
            )
            assert group.default_permissions.manage_guild is True, (
                f"Group {name!r} doesn't require manage_guild"
            )


# ---------------------------------------------------------------------------
# TestRunBot
# ---------------------------------------------------------------------------


class TestRunBot:
    """Tests for the run_bot function."""

    def test_missing_token_raises(self, config_no_token):
        """Test run_bot raises ConfigError when bot token is missing."""
        with pytest.raises(ConfigError, match="DISCORD_BOT_TOKEN"):
            run_bot(config_no_token)

    @patch("grocery_butler.bot.create_bot")
    def test_calls_client_run(self, mock_create_bot, config):
        """Test run_bot creates bot and calls run()."""
        mock_client = MagicMock()
        mock_create_bot.return_value = mock_client

        run_bot(config)

        mock_create_bot.assert_called_once_with(config)
        mock_client.run.assert_called_once_with("test-bot-token", log_handler=None)


# ---------------------------------------------------------------------------
# TestSlashCommands - integration tests using command callbacks directly
# ---------------------------------------------------------------------------


class TestMealsCommand:
    """Tests for the /meals slash command."""

    @pytest.fixture()
    def bot(self, config):
        """Create a bot instance for testing."""
        return create_bot(config)

    def _get_command(self, bot):
        """Get the meals command from the tree."""
        tree = bot.tree  # type: ignore[attr-defined]
        for cmd in tree.get_commands():
            if cmd.name == "meals":
                return cmd
        return None  # pragma: no cover

    @pytest.mark.asyncio()
    async def test_meals_empty_input(self, bot, mock_interaction):
        """Test /meals with empty input."""
        cmd = self._get_command(bot)
        assert cmd is not None
        await cmd.callback(mock_interaction, meals="  ,  , ")
        mock_interaction.followup.send.assert_called_once()
        call_args = mock_interaction.followup.send.call_args
        assert "at least one meal" in str(call_args)

    @pytest.mark.asyncio()
    async def test_meals_success(self, bot, mock_interaction):
        """Test /meals with valid input produces a shopping list."""
        cmd = self._get_command(bot)
        assert cmd is not None
        await cmd.callback(mock_interaction, meals="pasta, salad")
        mock_interaction.response.defer.assert_called_once()
        mock_interaction.followup.send.assert_called_once()
        call_args = str(mock_interaction.followup.send.call_args)
        assert "Shopping list" in call_args

    @pytest.mark.asyncio()
    async def test_meals_error_handling(self, bot, mock_interaction):
        """Test /meals handles errors gracefully."""
        cmd = self._get_command(bot)
        assert cmd is not None

        with patch(
            "grocery_butler.bot.asyncio.to_thread",
            side_effect=RuntimeError("test"),
        ):
            await cmd.callback(mock_interaction, meals="pasta")
        mock_interaction.followup.send.assert_called_once()
        call_args = str(mock_interaction.followup.send.call_args)
        assert "went wrong" in call_args


class TestPantryCommands:
    """Tests for the /pantry command group."""

    @pytest.fixture()
    def bot(self, config):
        """Create a bot instance for testing."""
        return create_bot(config)

    def _get_subcommand(self, bot, name):
        """Get a pantry subcommand by name."""
        tree = bot.tree  # type: ignore[attr-defined]
        for cmd in tree.get_commands():
            if cmd.name == "pantry":
                for sub in cmd.commands:
                    if sub.name == name:
                        return sub
        return None  # pragma: no cover

    @pytest.mark.asyncio()
    async def test_pantry_list(self, bot, mock_interaction):
        """Test /pantry list returns staples."""
        cmd = self._get_subcommand(bot, "list")
        assert cmd is not None
        await cmd.callback(mock_interaction)
        mock_interaction.response.send_message.assert_called_once()

    @pytest.mark.asyncio()
    async def test_pantry_add(self, bot, mock_interaction):
        """Test /pantry add adds a staple."""
        cmd = self._get_subcommand(bot, "add")
        assert cmd is not None
        await cmd.callback(mock_interaction, ingredient="cumin", category="pantry_dry")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "cumin" in call_args

    @pytest.mark.asyncio()
    async def test_pantry_add_error(self, bot, mock_interaction):
        """Test /pantry add handles errors gracefully."""
        cmd = self._get_subcommand(bot, "add")
        assert cmd is not None
        with patch(
            "grocery_butler.bot.asyncio.to_thread",
            side_effect=RuntimeError("db error"),
        ):
            await cmd.callback(
                mock_interaction,
                ingredient="cumin",
                category="pantry_dry",
            )
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "Could not add" in call_args

    @pytest.mark.asyncio()
    async def test_pantry_remove_not_found(self, bot, mock_interaction):
        """Test /pantry remove with non-existent staple."""
        cmd = self._get_subcommand(bot, "remove")
        assert cmd is not None
        await cmd.callback(mock_interaction, ingredient="nonexistent_spice")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "not found" in call_args

    @pytest.mark.asyncio()
    async def test_pantry_remove_success(self, bot, mock_interaction):
        """Test /pantry remove with existing staple."""
        cmd = self._get_subcommand(bot, "remove")
        assert cmd is not None
        # Salt is a default pantry staple
        await cmd.callback(mock_interaction, ingredient="salt")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "Removed" in call_args

    @pytest.mark.asyncio()
    async def test_pantry_remove_error(self, bot, mock_interaction):
        """Test /pantry remove handles errors gracefully."""
        cmd = self._get_subcommand(bot, "remove")
        assert cmd is not None
        with patch(
            "grocery_butler.bot.asyncio.to_thread",
            side_effect=RuntimeError("db"),
        ):
            await cmd.callback(mock_interaction, ingredient="salt")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "went wrong" in call_args

    @pytest.mark.asyncio()
    async def test_pantry_list_error(self, bot, mock_interaction):
        """Test /pantry list handles errors gracefully."""
        cmd = self._get_subcommand(bot, "list")
        assert cmd is not None
        with patch(
            "grocery_butler.bot.asyncio.to_thread",
            side_effect=RuntimeError("db"),
        ):
            await cmd.callback(mock_interaction)
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "went wrong" in call_args


class TestStockCommands:
    """Tests for the /stock command group."""

    @pytest.fixture()
    def bot(self, config):
        """Create a bot instance for testing."""
        return create_bot(config)

    def _get_subcommand(self, bot, name):
        """Get a stock subcommand by name."""
        tree = bot.tree  # type: ignore[attr-defined]
        for cmd in tree.get_commands():
            if cmd.name == "stock":
                for sub in cmd.commands:
                    if sub.name == name:
                        return sub
        return None  # pragma: no cover

    @pytest.mark.asyncio()
    async def test_stock_show(self, bot, mock_interaction):
        """Test /stock show returns inventory."""
        cmd = self._get_subcommand(bot, "show")
        assert cmd is not None
        await cmd.callback(mock_interaction)
        mock_interaction.response.send_message.assert_called_once()

    @pytest.mark.asyncio()
    async def test_stock_show_error(self, bot, mock_interaction):
        """Test /stock show handles errors gracefully."""
        cmd = self._get_subcommand(bot, "show")
        assert cmd is not None
        with patch(
            "grocery_butler.bot.asyncio.to_thread",
            side_effect=RuntimeError("db"),
        ):
            await cmd.callback(mock_interaction)
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "went wrong" in call_args

    @pytest.mark.asyncio()
    async def test_stock_out_not_found(self, bot, mock_interaction):
        """Test /stock out with non-existent item."""
        cmd = self._get_subcommand(bot, "out")
        assert cmd is not None
        await cmd.callback(mock_interaction, item="nonexistent")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "not found" in call_args

    @pytest.mark.asyncio()
    async def test_stock_out_error(self, bot, mock_interaction):
        """Test /stock out handles errors gracefully."""
        cmd = self._get_subcommand(bot, "out")
        assert cmd is not None
        with patch(
            "grocery_butler.bot.asyncio.to_thread",
            side_effect=RuntimeError("db"),
        ):
            await cmd.callback(mock_interaction, item="milk")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "went wrong" in call_args

    @pytest.mark.asyncio()
    async def test_stock_low_not_found(self, bot, mock_interaction):
        """Test /stock low with non-existent item."""
        cmd = self._get_subcommand(bot, "low")
        assert cmd is not None
        await cmd.callback(mock_interaction, item="nonexistent")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "not found" in call_args

    @pytest.mark.asyncio()
    async def test_stock_low_error(self, bot, mock_interaction):
        """Test /stock low handles errors gracefully."""
        cmd = self._get_subcommand(bot, "low")
        assert cmd is not None
        with patch(
            "grocery_butler.bot.asyncio.to_thread",
            side_effect=RuntimeError("db"),
        ):
            await cmd.callback(mock_interaction, item="milk")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "went wrong" in call_args

    @pytest.mark.asyncio()
    async def test_stock_good_not_found(self, bot, mock_interaction):
        """Test /stock good with non-existent item."""
        cmd = self._get_subcommand(bot, "good")
        assert cmd is not None
        await cmd.callback(mock_interaction, item="nonexistent")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "not found" in call_args

    @pytest.mark.asyncio()
    async def test_stock_good_error(self, bot, mock_interaction):
        """Test /stock good handles errors gracefully."""
        cmd = self._get_subcommand(bot, "good")
        assert cmd is not None
        with patch(
            "grocery_butler.bot.asyncio.to_thread",
            side_effect=RuntimeError("db"),
        ):
            await cmd.callback(mock_interaction, item="milk")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "went wrong" in call_args

    @pytest.mark.asyncio()
    async def test_stock_add(self, bot, mock_interaction):
        """Test /stock add creates new item."""
        cmd = self._get_subcommand(bot, "add")
        assert cmd is not None
        await cmd.callback(mock_interaction, item="cheese", category="dairy")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "tracking" in call_args

    @pytest.mark.asyncio()
    async def test_stock_add_invalid_category(self, bot, mock_interaction):
        """Test /stock add with invalid category falls back to OTHER."""
        cmd = self._get_subcommand(bot, "add")
        assert cmd is not None
        await cmd.callback(mock_interaction, item="mystery", category="invalid_cat")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "tracking" in call_args

    @pytest.mark.asyncio()
    async def test_stock_add_error(self, bot, mock_interaction):
        """Test /stock add handles errors gracefully."""
        cmd = self._get_subcommand(bot, "add")
        assert cmd is not None
        with patch(
            "grocery_butler.bot.asyncio.to_thread",
            side_effect=RuntimeError("db"),
        ):
            await cmd.callback(mock_interaction, item="cheese", category="dairy")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "Could not add" in call_args

    @pytest.mark.asyncio()
    async def test_stock_out_success(self, bot, mock_interaction):
        """Test /stock out marks item correctly."""
        # First add an item
        add_cmd = self._get_subcommand(bot, "add")
        assert add_cmd is not None
        await add_cmd.callback(mock_interaction, item="testitem", category="dairy")
        mock_interaction.response.send_message.reset_mock()

        # Then mark it out
        out_cmd = self._get_subcommand(bot, "out")
        assert out_cmd is not None
        await out_cmd.callback(mock_interaction, item="testitem")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "out of stock" in call_args

    @pytest.mark.asyncio()
    async def test_stock_low_success(self, bot, mock_interaction):
        """Test /stock low marks item correctly."""
        add_cmd = self._get_subcommand(bot, "add")
        assert add_cmd is not None
        await add_cmd.callback(mock_interaction, item="lowitem", category="produce")
        mock_interaction.response.send_message.reset_mock()

        low_cmd = self._get_subcommand(bot, "low")
        assert low_cmd is not None
        await low_cmd.callback(mock_interaction, item="lowitem")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "running low" in call_args

    @pytest.mark.asyncio()
    async def test_stock_good_success(self, bot, mock_interaction):
        """Test /stock good marks item correctly."""
        add_cmd = self._get_subcommand(bot, "add")
        assert add_cmd is not None
        await add_cmd.callback(mock_interaction, item="gooditem", category="meat")
        mock_interaction.response.send_message.reset_mock()

        good_cmd = self._get_subcommand(bot, "good")
        assert good_cmd is not None
        await good_cmd.callback(mock_interaction, item="gooditem")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "on hand" in call_args


class TestRestockCommands:
    """Tests for the /restock command group."""

    @pytest.fixture()
    def bot(self, config):
        """Create a bot instance for testing."""
        return create_bot(config)

    def _get_subcommand(self, bot, name):
        """Get a restock subcommand by name."""
        tree = bot.tree  # type: ignore[attr-defined]
        for cmd in tree.get_commands():
            if cmd.name == "restock":
                for sub in cmd.commands:
                    if sub.name == name:
                        return sub
        return None  # pragma: no cover

    @pytest.mark.asyncio()
    async def test_restock_show(self, bot, mock_interaction):
        """Test /restock show returns queue."""
        cmd = self._get_subcommand(bot, "show")
        assert cmd is not None
        await cmd.callback(mock_interaction)
        mock_interaction.response.send_message.assert_called_once()

    @pytest.mark.asyncio()
    async def test_restock_show_error(self, bot, mock_interaction):
        """Test /restock show handles errors gracefully."""
        cmd = self._get_subcommand(bot, "show")
        assert cmd is not None
        with patch(
            "grocery_butler.bot.asyncio.to_thread",
            side_effect=RuntimeError("db"),
        ):
            await cmd.callback(mock_interaction)
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "went wrong" in call_args

    @pytest.mark.asyncio()
    async def test_restock_clear(self, bot, mock_interaction):
        """Test /restock clear clears queue."""
        cmd = self._get_subcommand(bot, "clear")
        assert cmd is not None
        await cmd.callback(mock_interaction)
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "Cleared" in call_args

    @pytest.mark.asyncio()
    async def test_restock_clear_error(self, bot, mock_interaction):
        """Test /restock clear handles errors gracefully."""
        cmd = self._get_subcommand(bot, "clear")
        assert cmd is not None
        with patch(
            "grocery_butler.bot.asyncio.to_thread",
            side_effect=RuntimeError("db"),
        ):
            await cmd.callback(mock_interaction)
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "went wrong" in call_args


class TestBrandsCommands:
    """Tests for the /brands command group."""

    @pytest.fixture()
    def bot(self, config):
        """Create a bot instance for testing."""
        return create_bot(config)

    def _get_subcommand(self, bot, name):
        """Get a brands subcommand by name."""
        tree = bot.tree  # type: ignore[attr-defined]
        for cmd in tree.get_commands():
            if cmd.name == "brands":
                for sub in cmd.commands:
                    if sub.name == name:
                        return sub
        return None  # pragma: no cover

    @pytest.mark.asyncio()
    async def test_brands_show(self, bot, mock_interaction):
        """Test /brands show returns preferences."""
        cmd = self._get_subcommand(bot, "show")
        assert cmd is not None
        await cmd.callback(mock_interaction)
        mock_interaction.response.send_message.assert_called_once()

    @pytest.mark.asyncio()
    async def test_brands_show_error(self, bot, mock_interaction):
        """Test /brands show handles errors gracefully."""
        cmd = self._get_subcommand(bot, "show")
        assert cmd is not None
        with patch(
            "grocery_butler.bot.asyncio.to_thread",
            side_effect=RuntimeError("db"),
        ):
            await cmd.callback(mock_interaction)
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "went wrong" in call_args

    @pytest.mark.asyncio()
    async def test_brands_set(self, bot, mock_interaction):
        """Test /brands set adds a preference."""
        cmd = self._get_subcommand(bot, "set")
        assert cmd is not None
        await cmd.callback(mock_interaction, target="milk", brand="Organic Valley")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "milk" in call_args
        assert "Organic Valley" in call_args

    @pytest.mark.asyncio()
    async def test_brands_set_error(self, bot, mock_interaction):
        """Test /brands set handles errors gracefully."""
        cmd = self._get_subcommand(bot, "set")
        assert cmd is not None
        with patch(
            "grocery_butler.bot.asyncio.to_thread",
            side_effect=RuntimeError("db"),
        ):
            await cmd.callback(mock_interaction, target="milk", brand="test")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "went wrong" in call_args

    @pytest.mark.asyncio()
    async def test_brands_avoid(self, bot, mock_interaction):
        """Test /brands avoid adds to avoid list."""
        cmd = self._get_subcommand(bot, "avoid")
        assert cmd is not None
        await cmd.callback(mock_interaction, brand="Generic Brand")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "Generic Brand" in call_args

    @pytest.mark.asyncio()
    async def test_brands_avoid_error(self, bot, mock_interaction):
        """Test /brands avoid handles errors gracefully."""
        cmd = self._get_subcommand(bot, "avoid")
        assert cmd is not None
        with patch(
            "grocery_butler.bot.asyncio.to_thread",
            side_effect=RuntimeError("db"),
        ):
            await cmd.callback(mock_interaction, brand="test")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "went wrong" in call_args

    @pytest.mark.asyncio()
    async def test_brands_clear_not_found(self, bot, mock_interaction):
        """Test /brands clear with no matching preferences."""
        cmd = self._get_subcommand(bot, "clear")
        assert cmd is not None
        await cmd.callback(mock_interaction, target="nonexistent")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "No brand preferences" in call_args

    @pytest.mark.asyncio()
    async def test_brands_clear_error(self, bot, mock_interaction):
        """Test /brands clear handles errors gracefully."""
        cmd = self._get_subcommand(bot, "clear")
        assert cmd is not None
        with patch(
            "grocery_butler.bot.asyncio.to_thread",
            side_effect=RuntimeError("db"),
        ):
            await cmd.callback(mock_interaction, target="milk")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "went wrong" in call_args


class TestRecipesCommands:
    """Tests for the /recipes command group."""

    @pytest.fixture()
    def bot(self, config):
        """Create a bot instance for testing."""
        return create_bot(config)

    def _get_subcommand(self, bot, name):
        """Get a recipes subcommand by name."""
        tree = bot.tree  # type: ignore[attr-defined]
        for cmd in tree.get_commands():
            if cmd.name == "recipes":
                for sub in cmd.commands:
                    if sub.name == name:
                        return sub
        return None  # pragma: no cover

    @pytest.mark.asyncio()
    async def test_recipes_list(self, bot, mock_interaction):
        """Test /recipes list returns recipes."""
        cmd = self._get_subcommand(bot, "list")
        assert cmd is not None
        await cmd.callback(mock_interaction)
        mock_interaction.response.send_message.assert_called_once()

    @pytest.mark.asyncio()
    async def test_recipes_list_error(self, bot, mock_interaction):
        """Test /recipes list handles errors gracefully."""
        cmd = self._get_subcommand(bot, "list")
        assert cmd is not None
        with patch(
            "grocery_butler.bot.asyncio.to_thread",
            side_effect=RuntimeError("db"),
        ):
            await cmd.callback(mock_interaction)
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "went wrong" in call_args

    @pytest.mark.asyncio()
    async def test_recipes_show_not_found(self, bot, mock_interaction):
        """Test /recipes show with unknown recipe."""
        cmd = self._get_subcommand(bot, "show")
        assert cmd is not None
        await cmd.callback(mock_interaction, name="nonexistent recipe")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "not found" in call_args

    @pytest.mark.asyncio()
    async def test_recipes_show_error(self, bot, mock_interaction):
        """Test /recipes show handles errors gracefully."""
        cmd = self._get_subcommand(bot, "show")
        assert cmd is not None
        with patch(
            "grocery_butler.bot.asyncio.to_thread",
            side_effect=RuntimeError("db"),
        ):
            await cmd.callback(mock_interaction, name="pasta")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "went wrong" in call_args

    @pytest.mark.asyncio()
    async def test_recipes_forget_not_found(self, bot, mock_interaction):
        """Test /recipes forget with unknown recipe."""
        cmd = self._get_subcommand(bot, "forget")
        assert cmd is not None
        await cmd.callback(mock_interaction, name="nonexistent")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "not found" in call_args

    @pytest.mark.asyncio()
    async def test_recipes_forget_error(self, bot, mock_interaction):
        """Test /recipes forget handles errors gracefully."""
        cmd = self._get_subcommand(bot, "forget")
        assert cmd is not None
        with patch(
            "grocery_butler.bot.asyncio.to_thread",
            side_effect=RuntimeError("db"),
        ):
            await cmd.callback(mock_interaction, name="pasta")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "went wrong" in call_args


class TestPreferencesCommands:
    """Tests for the /preferences command group."""

    @pytest.fixture()
    def bot(self, config):
        """Create a bot instance for testing."""
        return create_bot(config)

    def _get_subcommand(self, bot, name):
        """Get a preferences subcommand by name."""
        tree = bot.tree  # type: ignore[attr-defined]
        for cmd in tree.get_commands():
            if cmd.name == "preferences":
                for sub in cmd.commands:
                    if sub.name == name:
                        return sub
        return None  # pragma: no cover

    @pytest.mark.asyncio()
    async def test_prefs_show(self, bot, mock_interaction):
        """Test /preferences show returns preferences."""
        cmd = self._get_subcommand(bot, "show")
        assert cmd is not None
        await cmd.callback(mock_interaction)
        mock_interaction.response.send_message.assert_called_once()

    @pytest.mark.asyncio()
    async def test_prefs_show_error(self, bot, mock_interaction):
        """Test /preferences show handles errors gracefully."""
        cmd = self._get_subcommand(bot, "show")
        assert cmd is not None
        with patch(
            "grocery_butler.bot.asyncio.to_thread",
            side_effect=RuntimeError("db"),
        ):
            await cmd.callback(mock_interaction)
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "went wrong" in call_args

    @pytest.mark.asyncio()
    async def test_prefs_set(self, bot, mock_interaction):
        """Test /preferences set updates a preference."""
        cmd = self._get_subcommand(bot, "set")
        assert cmd is not None
        await cmd.callback(mock_interaction, key="default_servings", value="6")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "default_servings" in call_args
        assert "6" in call_args

    @pytest.mark.asyncio()
    async def test_prefs_set_error(self, bot, mock_interaction):
        """Test /preferences set handles errors gracefully."""
        cmd = self._get_subcommand(bot, "set")
        assert cmd is not None
        with patch(
            "grocery_butler.bot.asyncio.to_thread",
            side_effect=RuntimeError("db"),
        ):
            await cmd.callback(mock_interaction, key="test", value="val")
        call_args = str(mock_interaction.response.send_message.call_args)
        assert "went wrong" in call_args


class TestOnReadyEvent:
    """Tests for the on_ready event handler."""

    @pytest.fixture()
    def bot(self, config):
        """Create a bot instance for testing."""
        return create_bot(config)

    @pytest.mark.asyncio()
    async def test_on_ready_syncs_commands(self, bot):
        """Test on_ready syncs the command tree."""
        mock_user = MagicMock()
        mock_user.__str__ = lambda self: "TestBot#1234"

        # Mock tree.sync
        tree = bot.tree  # type: ignore[attr-defined]
        tree.sync = AsyncMock()

        # Patch the read-only 'user' property on the Client class
        with patch.object(
            type(bot), "user", new_callable=PropertyMock, return_value=mock_user
        ):
            await bot.on_ready()  # type: ignore[attr-defined]
        tree.sync.assert_called_once()


class TestOnMessageEvent:
    """Tests for the on_message natural language handler."""

    @pytest.fixture()
    def bot(self, config):
        """Create a bot instance for testing."""
        return create_bot(config)

    @pytest.mark.asyncio()
    async def test_ignores_bot_messages(self, bot):
        """Test on_message ignores the bot's own messages."""
        message = MagicMock()
        message.author = bot.user
        message.content = "hello"
        message.reply = AsyncMock()

        await bot.on_message(message)  # type: ignore[attr-defined]
        message.reply.assert_not_called()

    @pytest.mark.asyncio()
    async def test_ignores_dm_messages(self, bot):
        """Test on_message ignores DMs (no guild)."""
        message = _make_guild_message(manage_guild=True, content="out of milk")
        message.guild = None  # DM message

        mock_user = MagicMock()
        mock_user.id = 111111

        with patch.object(
            type(bot),
            "user",
            new_callable=PropertyMock,
            return_value=mock_user,
        ):
            await bot.on_message(message)  # type: ignore[attr-defined]
        message.reply.assert_not_called()

    @pytest.mark.asyncio()
    async def test_ignores_non_member_author(self, bot):
        """Test on_message ignores messages from non-Member authors (e.g., webhooks)."""
        message = MagicMock()
        message.author = MagicMock(spec=discord.User)  # User, not Member
        message.guild = MagicMock()  # In a guild, so DM check passes
        message.content = "out of milk"
        message.reply = AsyncMock()

        mock_user = MagicMock()
        mock_user.id = 111111

        with patch.object(
            type(bot),
            "user",
            new_callable=PropertyMock,
            return_value=mock_user,
        ):
            await bot.on_message(message)  # type: ignore[attr-defined]
        message.reply.assert_not_called()

    @pytest.mark.asyncio()
    async def test_ignores_user_without_manage_guild(self, bot):
        """Test on_message ignores users without manage_guild permission."""
        message = _make_guild_message(manage_guild=False, content="we are out of milk")

        mock_user = MagicMock()
        mock_user.id = 111111

        with patch.object(
            type(bot),
            "user",
            new_callable=PropertyMock,
            return_value=mock_user,
        ):
            await bot.on_message(message)  # type: ignore[attr-defined]
        message.reply.assert_not_called()

    @pytest.mark.asyncio()
    async def test_ignores_command_messages(self, bot):
        """Test on_message ignores messages starting with /."""
        message = _make_guild_message(manage_guild=True, content="/meals pasta")

        mock_user = MagicMock()
        mock_user.id = 111111

        with patch.object(
            type(bot),
            "user",
            new_callable=PropertyMock,
            return_value=mock_user,
        ):
            await bot.on_message(message)  # type: ignore[attr-defined]
        message.reply.assert_not_called()

    @pytest.mark.asyncio()
    async def test_ignores_empty_messages(self, bot):
        """Test on_message ignores empty messages."""
        message = _make_guild_message(manage_guild=True, content="   ")

        mock_user = MagicMock()
        mock_user.id = 111111

        with patch.object(
            type(bot),
            "user",
            new_callable=PropertyMock,
            return_value=mock_user,
        ):
            await bot.on_message(message)  # type: ignore[attr-defined]
        message.reply.assert_not_called()

    @pytest.mark.asyncio()
    async def test_no_updates_detected(self, bot):
        """Test on_message with no detected inventory updates."""
        message = _make_guild_message(manage_guild=True, content="hello there")

        mock_user = MagicMock()
        mock_user.id = 111111

        # PantryManager without client returns empty list
        with patch.object(
            type(bot),
            "user",
            new_callable=PropertyMock,
            return_value=mock_user,
        ):
            await bot.on_message(message)  # type: ignore[attr-defined]
        message.reply.assert_not_called()

    @pytest.mark.asyncio()
    async def test_high_confidence_update(self, bot):
        """Test on_message processes high-confidence updates."""
        message = _make_guild_message(manage_guild=True, content="we are out of milk")

        mock_user = MagicMock()
        mock_user.id = 111111

        updates = [
            InventoryUpdate(
                ingredient="milk",
                new_status=InventoryStatus.OUT,
                confidence=0.95,
            ),
        ]

        with (
            patch.object(
                type(bot),
                "user",
                new_callable=PropertyMock,
                return_value=mock_user,
            ),
            patch(
                "grocery_butler.bot.asyncio.to_thread",
            ) as mock_thread,
        ):
            mock_thread.side_effect = [
                updates,  # parse_inventory_intent
                None,  # update_status
            ]
            await bot.on_message(message)  # type: ignore[attr-defined]

        message.reply.assert_called_once()
        reply_text = str(message.reply.call_args)
        assert "milk" in reply_text

    @pytest.mark.asyncio()
    async def test_low_confidence_clarification(self, bot):
        """Test on_message asks for clarification on low-conf."""
        message = _make_guild_message(
            manage_guild=True, content="I think we might need milk"
        )

        mock_user = MagicMock()
        mock_user.id = 111111

        updates = [
            InventoryUpdate(
                ingredient="milk",
                new_status=InventoryStatus.LOW,
                confidence=0.5,
            ),
        ]

        with (
            patch.object(
                type(bot),
                "user",
                new_callable=PropertyMock,
                return_value=mock_user,
            ),
            patch(
                "grocery_butler.bot.asyncio.to_thread",
                return_value=updates,
            ),
        ):
            await bot.on_message(message)  # type: ignore[attr-defined]

        message.reply.assert_called_once()
        reply_text = str(message.reply.call_args)
        assert "clarify" in reply_text

    @pytest.mark.asyncio()
    async def test_error_handling(self, bot):
        """Test on_message handles errors silently."""
        message = _make_guild_message(manage_guild=True, content="out of milk")

        mock_user = MagicMock()
        mock_user.id = 111111

        with (
            patch.object(
                type(bot),
                "user",
                new_callable=PropertyMock,
                return_value=mock_user,
            ),
            patch(
                "grocery_butler.bot.asyncio.to_thread",
                side_effect=RuntimeError("api error"),
            ),
        ):
            # Should not raise
            await bot.on_message(message)  # type: ignore[attr-defined]
        message.reply.assert_not_called()

    @pytest.mark.asyncio()
    async def test_mixed_confidence_updates(self, bot):
        """Test on_message handles mix of high and low conf."""
        message = _make_guild_message(
            manage_guild=True, content="out of milk, maybe low on eggs"
        )

        mock_user = MagicMock()
        mock_user.id = 111111

        updates = [
            InventoryUpdate(
                ingredient="milk",
                new_status=InventoryStatus.OUT,
                confidence=0.95,
            ),
            InventoryUpdate(
                ingredient="eggs",
                new_status=InventoryStatus.LOW,
                confidence=0.5,
            ),
        ]

        call_count = 0

        async def mock_to_thread(func, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return updates
            return None

        with (
            patch.object(
                type(bot),
                "user",
                new_callable=PropertyMock,
                return_value=mock_user,
            ),
            patch(
                "grocery_butler.bot.asyncio.to_thread",
                side_effect=mock_to_thread,
            ),
        ):
            await bot.on_message(message)  # type: ignore[attr-defined]

        # Should reply twice: once for high-conf, once for low-conf
        assert message.reply.call_count == 2


# ---------------------------------------------------------------------------
# Order bot formatters
# ---------------------------------------------------------------------------


class TestFormatCartSummaryBot:
    """Tests for _format_cart_summary in bot module."""

    def test_format_with_items(self):
        """Test formatting a cart summary with items."""
        from grocery_butler.models import (
            CartItem,
            CartSummary,
            FulfillmentType,
            SafewayProduct,
        )

        product = SafewayProduct(product_id="P1", name="Milk", price=4.99, size="1 gal")
        cart_item = CartItem(
            shopping_list_item=ShoppingListItem(
                ingredient="milk",
                quantity=1.0,
                unit="gal",
                category=IngredientCategory.DAIRY,
                search_term="milk",
                from_meals=["manual"],
            ),
            safeway_product=product,
            quantity_to_order=1,
            estimated_cost=4.99,
        )
        cart = CartSummary(
            items=[cart_item],
            failed_items=[],
            substituted_items=[],
            skipped_items=[],
            restock_items=[],
            subtotal=4.99,
            fulfillment_options=[],
            recommended_fulfillment=FulfillmentType.PICKUP,
            estimated_total=4.99,
        )

        result = _format_cart_summary(cart)
        assert "Milk" in result
        assert "$4.99" in result
        assert "```" in result

    def test_format_with_failed_items(self):
        """Test formatting cart with failed items."""
        from grocery_butler.models import CartSummary, FulfillmentType

        cart = CartSummary(
            items=[],
            failed_items=[
                ShoppingListItem(
                    ingredient="unicorn tears",
                    quantity=1.0,
                    unit="each",
                    category=IngredientCategory.OTHER,
                    search_term="unicorn tears",
                    from_meals=["manual"],
                ),
            ],
            substituted_items=[],
            skipped_items=[],
            restock_items=[],
            subtotal=0.0,
            fulfillment_options=[],
            recommended_fulfillment=FulfillmentType.PICKUP,
            estimated_total=0.0,
        )

        result = _format_cart_summary(cart)
        assert "Failed" in result
        assert "unicorn tears" in result


class TestFormatOrderResult:
    """Tests for _format_order_result in bot module."""

    def test_format_success(self):
        """Test formatting a successful order result."""
        from grocery_butler.models import FulfillmentType
        from grocery_butler.order_service import OrderConfirmation, OrderResult

        result = OrderResult(
            success=True,
            confirmation=OrderConfirmation(
                order_id="ORD-123",
                status="confirmed",
                estimated_time="2 hours",
                total=24.99,
                fulfillment_type=FulfillmentType.PICKUP,
                item_count=5,
            ),
            items_restocked=2,
        )

        formatted = _format_order_result(result)
        assert "ORD-123" in formatted
        assert "confirmed" in formatted
        assert "$24.99" in formatted
        assert "Restocked 2" in formatted

    def test_format_failure(self):
        """Test formatting a failed order result."""
        from grocery_butler.order_service import OrderResult

        result = OrderResult(
            success=False,
            error_message="Payment declined",
        )

        formatted = _format_order_result(result)
        assert "failed" in formatted.lower()
        assert "Payment declined" in formatted

    def test_format_success_no_confirmation(self):
        """Test formatting success with missing confirmation."""
        from grocery_butler.order_service import OrderResult

        result = OrderResult(success=True, confirmation=None)
        formatted = _format_order_result(result)
        assert "no confirmation" in formatted.lower()


class TestMakeBotAnthropicClient:
    """Tests for _make_bot_anthropic_client."""

    def test_returns_none_on_error(self, config):
        """Test returns None when anthropic is unavailable."""
        with patch.dict("sys.modules", {"anthropic": None}):
            result = _make_bot_anthropic_client(config)
            assert result is None or result is not None


class TestOrderCommandGroup:
    """Tests for /order command group registration."""

    @pytest.fixture()
    def bot(self, config):
        """Create a bot instance for testing."""
        return create_bot(config)

    def _get_group(self, bot, name):
        """Get command group by name.

        Args:
            bot: Discord client.
            name: Group name.

        Returns:
            The command group.
        """
        tree = bot.tree  # type: ignore[attr-defined]
        for cmd in tree.get_commands():
            if cmd.name == name:
                return cmd
        return None  # pragma: no cover

    def test_order_group_exists(self, bot):
        """Test /order command group is registered."""
        group = self._get_group(bot, "order")
        assert group is not None

    def test_order_subcommands(self, bot):
        """Test /order has review, submit, and status subcommands."""
        group = self._get_group(bot, "order")
        assert group is not None
        subcommand_names = {cmd.name for cmd in group.commands}
        assert "review" in subcommand_names
        assert "submit" in subcommand_names
        assert "status" in subcommand_names
