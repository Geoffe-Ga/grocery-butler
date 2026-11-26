"""The dashboard's JavaScript does what its buttons promise (issue #231).

Each test drives a control in a real browser and then checks stored
state, usually after a reload, so a control that only redraws itself
fails. Every test also fails on any console error or Content Security
Policy violation in the pages it visits (see the ``page`` fixture).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Dialog, Locator, Page, expect

from grocery_butler.fulfillment.handoffs import Handoffs
from grocery_butler.fulfillment.registry import default_registry
from grocery_butler.household_items import HouseholdItemStore
from grocery_butler.models import (
    Ingredient,
    IngredientCategory,
    InventoryItem,
    InventoryStatus,
    NeedReasonKind,
    ParsedMeal,
)
from grocery_butler.pantry_manager import PantryManager
from grocery_butler.recipe_store import RecipeStore
from grocery_butler.replenishment import ReplenishmentNeeds
from grocery_butler.shopping_plans import ShoppingPlans

if TYPE_CHECKING:
    from tests.browser.conftest import PageProblems

pytestmark = pytest.mark.browser

UPDATE_URL = "**/inventory/update"
ACTIVE = re.compile(r"\bactive\b")
OPEN = re.compile(r"\bopen\b")


@pytest.fixture()
def inventory(db_path: str, base_url: str) -> PantryManager:
    """Track milk and eggs as on hand, and return the manager to read back.

    Depends on ``base_url`` so the app has migrated the database first.
    """
    manager = PantryManager(db_path)
    for name, display in (("milk", "Milk"), ("eggs", "Eggs")):
        manager.add_item(
            InventoryItem(
                ingredient=name,
                display_name=display,
                category=IngredientCategory.DAIRY,
                status=InventoryStatus.ON_HAND,
            )
        )
    return manager


def _status_button(page: Page, ingredient: str, status: str) -> Locator:
    """Locate one status button of one inventory card."""
    return page.locator(
        f'.status-buttons[data-ingredient="{ingredient}"]'
        f' .btn-status[data-status="{status}"]'
    )


def _stored_status(manager: PantryManager, ingredient: str) -> InventoryStatus:
    """Return an item's status as the database holds it."""
    item = manager.get_item(ingredient)
    assert item is not None
    return item.status


class TestInventoryStatusToggle:
    """Tapping a status saves it; it is not only drawn."""

    def test_a_tap_is_saved_and_survives_a_reload(
        self, page: Page, inventory: PantryManager
    ) -> None:
        """The request succeeds, the button lights, and the server agrees."""
        page.goto("/inventory")
        expect(_status_button(page, "milk", "on_hand")).to_have_class(ACTIVE)

        with page.expect_response(UPDATE_URL) as update:
            _status_button(page, "milk", "out").click()

        assert update.value.status == 200
        expect(_status_button(page, "milk", "out")).to_have_class(ACTIVE)
        assert _stored_status(inventory, "milk") is InventoryStatus.OUT
        assert _stored_status(inventory, "eggs") is InventoryStatus.ON_HAND

        page.reload()
        expect(_status_button(page, "milk", "out")).to_have_class(ACTIVE)
        expect(_status_button(page, "milk", "on_hand")).not_to_have_class(ACTIVE)

    def test_without_a_session_the_tap_is_refused_and_says_so(
        self, page: Page, inventory: PantryManager, problems: PageProblems
    ) -> None:
        """No cookie, no change, and the person is told rather than misled."""
        problems.expect("400")
        page.goto("/inventory")
        page.context.clear_cookies()

        with page.expect_event("dialog") as shown:
            _status_button(page, "milk", "out").click()
        dialog: Dialog = shown.value
        message = dialog.message
        dialog.accept()

        assert message.startswith("Error: ")
        expect(_status_button(page, "milk", "on_hand")).to_have_class(ACTIVE)
        expect(_status_button(page, "milk", "out")).not_to_have_class(ACTIVE)
        assert _stored_status(inventory, "milk") is InventoryStatus.ON_HAND

    @pytest.mark.usefixtures("inventory")
    def test_the_search_box_filters_the_cards(self, page: Page) -> None:
        """Typing hides what doesn't match and clearing brings it back."""
        page.goto("/inventory")
        milk = page.locator('.inventory-card[data-ingredient="milk"]')
        eggs = page.locator('.inventory-card[data-ingredient="eggs"]')

        page.locator("#inventory-search").fill("mil")
        expect(milk).to_be_visible()
        expect(eggs).to_be_hidden()

        page.locator("#inventory-search").fill("")
        expect(eggs).to_be_visible()


class TestForms:
    """Forms post with the page's own CSRF token and change stored data."""

    def test_adding_a_pantry_staple(self, page: Page, db_path: str) -> None:
        """The real form, submitted by a real click, stores the staple."""
        page.goto("/pantry")
        page.locator("#ingredient").fill("cumin")
        page.locator("#category").select_option("pantry_dry")

        page.locator(".add-item-form button[type=submit]").click()

        expect(page.locator(".pantry-card-name", has_text="Cumin")).to_be_visible()
        assert "cumin" in RecipeStore(db_path).get_pantry_staple_names()

    def test_the_add_row_button_makes_rows_the_server_accepts(
        self, page: Page, db_path: str
    ) -> None:
        """A row built by the script is saved like one from the template."""
        page.goto("/recipes/add")
        page.locator("#name").fill("Bean Toast")
        page.locator("#ing_name_0").fill("bread")
        page.locator("#ing_unit_0").fill("slice")

        page.locator("#add-ingredient-row").click()
        expect(page.locator(".ingredient-row")).to_have_count(2)
        page.locator("#ing_name_1").fill("kidney beans")
        page.locator("#ing_unit_1").fill("can")
        page.locator("#ing_category_1").select_option("pantry_dry")
        page.locator("form button[type=submit]", has_text="Save Recipe").click()

        expect(page.locator("body")).to_contain_text("Bean Toast")
        saved = RecipeStore(db_path).find_recipe("bean toast")
        assert saved is not None
        assert sorted(i.ingredient for i in saved.purchase_items) == [
            "bread",
            "kidney beans",
        ]

    @pytest.mark.parametrize(
        ("answer", "still_there"), [("dismiss", True), ("accept", False)]
    )
    def test_deleting_a_recipe_asks_first(
        self, page: Page, db_path: str, base_url: str, answer: str, still_there: bool
    ) -> None:
        """Cancel keeps the recipe; OK deletes it. The script does the asking."""
        store = RecipeStore(db_path)
        recipe_id = store.save_recipe(_one_ingredient_meal())
        asked: list[str] = []

        def respond(dialog: Dialog) -> None:
            asked.append(dialog.message)
            getattr(dialog, answer)()

        page.goto(f"/recipes/{recipe_id}")
        page.once("dialog", respond)
        delete = page.locator("form[data-confirm] button[type=submit]")
        if still_there:
            delete.click()
            expect(page).to_have_url(f"{base_url}/recipes/{recipe_id}")
        else:
            with page.expect_navigation():
                delete.click()

        assert asked == ["Delete this recipe?"]
        assert (store.find_recipe("bean toast") is not None) is still_there


class TestNavigation:
    """The phone menu opens and closes."""

    def test_the_menu_button_toggles_the_links(self, page: Page) -> None:
        """At phone width the links are behind the button."""
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto("/")
        toggle = page.locator(".nav-toggle")
        links = page.locator(".nav-links")

        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(links).to_have_class(OPEN)

        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(links).not_to_have_class(OPEN)


class TestHandoffPage:
    """The Instacart button goes through our own tracked link."""

    def test_the_button_points_at_the_tracked_link_and_records_the_open(
        self, page: Page, db_path: str, base_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Following it marks the attempt opened and sends the person on."""
        monkeypatch.setenv("INSTACART_ENABLED", "true")
        monkeypatch.delenv("INSTACART_ENV", raising=False)
        handoffs = Handoffs(db_path, default_registry().get)
        item = HouseholdItemStore(db_path).resolve_or_create(
            "oat milk", actor="browser-test"
        )
        ReplenishmentNeeds(db_path).add_reason(
            item.id,
            NeedReasonKind.MANUAL_REQUEST,
            source_key="browser",
            actor="browser-test",
        )
        plan = ShoppingPlans(db_path).build([], actor="browser-test")
        draft = handoffs.draft(plan.version, "instacart", actor="browser-test")
        handoffs.approve(draft.id, draft.payload_hash, actor="browser-test")
        attempt = handoffs.execute(
            draft.id, actor="browser-test", idempotency_key="browser-handoff-1"
        )

        page.goto("/handoff")
        button = page.locator("a", has_text="Shop on Instacart")
        expect(button).to_have_attribute("href", f"/handoff/attempts/{attempt.id}/open")

        followed = page.request.get(
            f"{base_url}/handoff/attempts/{attempt.id}/open", max_redirects=0
        )

        assert followed.status == 302
        assert followed.headers["location"] == attempt.external_url
        opened = handoffs.get_attempt(attempt.id)
        assert opened is not None
        assert opened.opened_at is not None


def _one_ingredient_meal() -> ParsedMeal:
    """Return a small recipe to save and then delete."""
    return ParsedMeal(
        name="bean toast",
        servings=2,
        known_recipe=True,
        needs_confirmation=False,
        purchase_items=[
            Ingredient(
                ingredient="bread",
                quantity=2.0,
                unit="slice",
                category=IngredientCategory.BAKERY,
            )
        ],
        pantry_items=[],
    )
