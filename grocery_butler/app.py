"""Flask web application for Grocery Butler dashboard.

Provides a web interface for managing household inventory, viewing
recipes, shopping lists, pantry staples, brand preferences, and
user preferences. Uses SQLite with WAL mode for concurrent read access.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import TYPE_CHECKING

from flask import Flask, flash, g, jsonify, redirect, render_template, request, url_for

if TYPE_CHECKING:
    from werkzeug.wrappers import Response

    from grocery_butler.models import Ingredient

from grocery_butler.db import get_connection, init_db
from grocery_butler.models import (
    BrandMatchType,
    BrandPreference,
    BrandPreferenceType,
    IngredientCategory,
    InventoryItem,
    InventoryStatus,
    parse_unit,
)
from grocery_butler.pantry_manager import PantryManager
from grocery_butler.recipe_store import RecipeStore

logger = logging.getLogger(__name__)


def create_app(db_path: str = "mealbot.db") -> Flask:
    """Create and configure the Flask application.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        Configured Flask application instance.
    """
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.config["DATABASE_PATH"] = db_path
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", os.urandom(32).hex())

    init_db(db_path)

    _register_teardown(app)
    _register_error_handlers(app)
    _register_context_processors(app)
    _register_routes(app)

    return app


def _get_db() -> sqlite3.Connection:
    """Get a database connection for the current request.

    Returns:
        SQLite connection with WAL mode enabled.
    """
    if "db" not in g:
        from flask import current_app

        g.db = get_connection(current_app.config["DATABASE_PATH"])
    conn: sqlite3.Connection = g.db
    return conn


def _close_db(exc: BaseException | None = None) -> None:
    """Close the database connection at the end of a request.

    Args:
        exc: Exception that occurred during request handling, if any.
    """
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _register_teardown(app: Flask) -> None:
    """Register the database teardown handler.

    Args:
        app: Flask application instance.
    """
    app.teardown_appcontext(_close_db)


def _register_error_handlers(app: Flask) -> None:
    """Register HTTP error handler pages.

    Args:
        app: Flask application instance.
    """

    @app.errorhandler(400)
    def bad_request(error: Exception) -> tuple[str, int]:
        """Handle 400 Bad Request errors.

        Args:
            error: The exception that triggered the error.

        Returns:
            Tuple of rendered template and HTTP status code.
        """
        return render_template("error.html", code=400, message="Bad Request"), 400

    @app.errorhandler(404)
    def not_found(error: Exception) -> tuple[str, int]:
        """Handle 404 Not Found errors.

        Args:
            error: The exception that triggered the error.

        Returns:
            Tuple of rendered template and HTTP status code.
        """
        return render_template("error.html", code=404, message="Page Not Found"), 404

    @app.errorhandler(500)
    def server_error(error: Exception) -> tuple[str, int]:
        """Handle 500 Internal Server Error.

        Args:
            error: The exception that triggered the error.

        Returns:
            Tuple of rendered template and HTTP status code.
        """
        return (
            render_template("error.html", code=500, message="Internal Server Error"),
            500,
        )


def _register_context_processors(app: Flask) -> None:
    """Register Jinja2 context processors.

    Args:
        app: Flask application instance.
    """

    @app.context_processor
    def inject_nav() -> dict[str, list[dict[str, str]]]:
        """Inject navigation items into all templates.

        Returns:
            Dict with nav_items list for the template context.
        """
        return {
            "nav_items": [
                {"label": "Dashboard", "url": "/", "endpoint": "dashboard"},
                {"label": "Recipes", "url": "/recipes", "endpoint": "recipes"},
                {"label": "Inventory", "url": "/inventory", "endpoint": "inventory"},
                {"label": "Pantry", "url": "/pantry", "endpoint": "pantry"},
                {
                    "label": "Shopping List",
                    "url": "/shopping-list",
                    "endpoint": "shopping_list",
                },
                {"label": "Brands", "url": "/brands", "endpoint": "brands"},
                {
                    "label": "Preferences",
                    "url": "/preferences",
                    "endpoint": "preferences",
                },
            ]
        }


def _register_routes(app: Flask) -> None:
    """Register all application routes.

    Args:
        app: Flask application instance.
    """
    _register_dashboard_routes(app)
    _register_inventory_routes(app)
    _register_recipe_routes(app)
    _register_shopping_list_routes(app)
    _register_pantry_routes(app)
    _register_brand_routes(app)
    _register_preferences_routes(app)


def _register_dashboard_routes(app: Flask) -> None:
    """Register dashboard page routes.

    Args:
        app: Flask application instance.
    """

    @app.route("/")
    def dashboard() -> str:
        """Render the dashboard page with stats and restock queue.

        Returns:
            Rendered dashboard HTML.
        """
        db_path = app.config["DATABASE_PATH"]
        pantry_mgr = PantryManager(db_path)
        recipe_store = RecipeStore(db_path)

        restock_queue = pantry_mgr.get_restock_queue()
        inventory = pantry_mgr.get_inventory()
        recipes = recipe_store.list_recipes()

        return render_template(
            "dashboard.html",
            active_page="dashboard",
            restock_queue=restock_queue,
            inventory_count=len(inventory),
            recipe_count=len(recipes),
        )


def _register_inventory_routes(app: Flask) -> None:
    """Register inventory page routes.

    Args:
        app: Flask application instance.
    """

    @app.route("/inventory")
    def inventory() -> str:
        """Render the inventory management page.

        Returns:
            Rendered inventory HTML.
        """
        db_path = app.config["DATABASE_PATH"]
        pantry_mgr = PantryManager(db_path)
        items = pantry_mgr.get_inventory()
        categories = [cat.value for cat in IngredientCategory]

        return render_template(
            "inventory.html",
            active_page="inventory",
            items=items,
            categories=categories,
            statuses=[s.value for s in InventoryStatus],
        )

    @app.route("/inventory/update", methods=["POST"])
    def inventory_update() -> tuple[Response, int] | tuple[str, int]:
        """Update an inventory item's status via AJAX.

        Expects JSON body with ``ingredient`` and ``status`` fields.

        Returns:
            JSON response with success flag and updated status, or error.
        """
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({"success": False, "error": "Invalid JSON"}), 400

        ingredient = data.get("ingredient")
        status_value = data.get("status")

        if not ingredient or not status_value:
            return (
                jsonify({"success": False, "error": "Missing ingredient or status"}),
                400,
            )

        valid_statuses = {s.value for s in InventoryStatus}
        if status_value not in valid_statuses:
            return (
                jsonify({"success": False, "error": f"Invalid status: {status_value}"}),
                400,
            )

        db_path = app.config["DATABASE_PATH"]
        pantry_mgr = PantryManager(db_path)
        new_status = InventoryStatus(status_value)

        item = pantry_mgr.get_item(ingredient)
        if item is None:
            return (
                jsonify({"success": False, "error": "Item not found"}),
                404,
            )

        pantry_mgr.update_status(ingredient, new_status)
        return jsonify({"success": True, "status": new_status.value}), 200

    @app.route("/inventory/add", methods=["POST"])
    def inventory_add() -> Response:
        """Add a new tracked inventory item via form POST.

        Expects form fields: ``ingredient``, ``display_name``, ``category``,
        ``status``.

        Returns:
            Redirect to the inventory page.
        """
        ingredient = request.form.get("ingredient", "").strip()
        display_name = request.form.get("display_name", "").strip()
        category = request.form.get("category", "").strip()
        status = request.form.get("status", "on_hand").strip()

        if not ingredient:
            flash("Ingredient name is required.", "error")
            return redirect(url_for("inventory"))

        if not display_name:
            display_name = ingredient.replace("_", " ").title()

        cat_value: IngredientCategory | None = None
        if category:
            try:
                cat_value = IngredientCategory(category)
            except ValueError:
                flash(f"Invalid category: {category}", "error")
                return redirect(url_for("inventory"))

        try:
            status_enum = InventoryStatus(status)
        except ValueError:
            flash(f"Invalid status: {status}", "error")
            return redirect(url_for("inventory"))

        item = InventoryItem(
            ingredient=ingredient.lower(),
            display_name=display_name,
            category=cat_value,
            status=status_enum,
        )

        db_path = app.config["DATABASE_PATH"]
        pantry_mgr = PantryManager(db_path)

        try:
            pantry_mgr.add_item(item)
            flash(f"Added {display_name} to inventory.", "success")
        except sqlite3.IntegrityError:
            flash(f"{display_name} already exists in inventory.", "error")

        return redirect(url_for("inventory"))


def _register_recipe_routes(app: Flask) -> None:
    """Register recipe page routes.

    Args:
        app: Flask application instance.
    """

    @app.route("/recipes")
    def recipes() -> str:
        """Render the recipes list page.

        Returns:
            Rendered recipes HTML.
        """
        db_path = app.config["DATABASE_PATH"]
        recipe_store = RecipeStore(db_path)
        recipe_list = recipe_store.list_recipes()

        # Add ingredient count for each recipe
        for recipe in recipe_list:
            recipe_id = recipe["id"]
            meal = recipe_store.get_recipe_by_id(int(str(recipe_id)))
            if meal is not None:
                recipe["ingredient_count"] = len(meal.purchase_items) + len(
                    meal.pantry_items
                )
            else:
                recipe["ingredient_count"] = 0

        return render_template(
            "recipes.html",
            active_page="recipes",
            recipes=recipe_list,
        )

    @app.route("/recipes/add", methods=["GET", "POST"])
    def recipe_add() -> str | Response:
        """Handle recipe add form display and submission.

        GET: Render the add recipe form.
        POST: Process form data and create a new recipe.

        Returns:
            Rendered form HTML or redirect to recipe detail.
        """
        if request.method == "GET":
            categories = [cat.value for cat in IngredientCategory]
            return render_template(
                "recipe_add.html",
                active_page="recipes",
                categories=categories,
            )

        return _handle_recipe_add_post(app)

    @app.route("/recipes/<int:recipe_id>")
    def recipe_detail(recipe_id: int) -> str | tuple[str, int]:
        """Render a single recipe's detail page.

        Args:
            recipe_id: Database ID of the recipe.

        Returns:
            Rendered recipe detail HTML or 404 error page.
        """
        db_path = app.config["DATABASE_PATH"]
        recipe_store = RecipeStore(db_path)
        meal = recipe_store.get_recipe_by_id(recipe_id)

        if meal is None:
            return (
                render_template(
                    "error.html",
                    code=404,
                    message="Recipe not found",
                ),
                404,
            )

        return render_template(
            "recipe_detail.html",
            active_page="recipes",
            recipe=meal,
            recipe_id=recipe_id,
        )

    @app.route("/recipes/<int:recipe_id>/delete", methods=["POST"])
    def recipe_delete(recipe_id: int) -> Response:
        """Delete a recipe and redirect to the recipes list.

        Args:
            recipe_id: Database ID of the recipe to delete.

        Returns:
            Redirect to the recipes page.
        """
        db_path = app.config["DATABASE_PATH"]
        recipe_store = RecipeStore(db_path)
        recipe_store.delete_recipe(recipe_id)
        flash("Recipe deleted.", "success")
        return redirect(url_for("recipes"))


def _handle_recipe_add_post(app: Flask) -> Response:
    """Process the recipe add form POST submission.

    Args:
        app: Flask application instance.

    Returns:
        Redirect to the new recipe detail or back to the form.
    """
    from grocery_butler.models import ParsedMeal

    name = request.form.get("name", "").strip()
    servings_raw = request.form.get("servings", "4").strip()

    if not name:
        flash("Recipe name is required.", "error")
        return redirect(url_for("recipe_add"))

    try:
        servings = int(servings_raw)
    except ValueError:
        flash("Servings must be a number.", "error")
        return redirect(url_for("recipe_add"))

    ingredients = _parse_ingredient_form_rows()

    if not ingredients:
        flash("At least one ingredient is required.", "error")
        return redirect(url_for("recipe_add"))

    meal = ParsedMeal(
        name=name,
        servings=servings,
        known_recipe=True,
        needs_confirmation=False,
        purchase_items=[i for i in ingredients if not i.is_pantry_item],
        pantry_items=[i for i in ingredients if i.is_pantry_item],
    )

    db_path = app.config["DATABASE_PATH"]
    recipe_store = RecipeStore(db_path)

    try:
        recipe_id = recipe_store.save_recipe(meal)
        flash(f"Recipe '{name}' saved.", "success")
        return redirect(url_for("recipe_detail", recipe_id=recipe_id))
    except sqlite3.IntegrityError:
        flash(f"A recipe named '{name}' already exists.", "error")
        return redirect(url_for("recipe_add"))


def _parse_ingredient_form_rows() -> list[Ingredient]:
    """Parse ingredient rows from the add recipe form.

    Returns:
        List of Ingredient models from form data.
    """
    from grocery_butler.models import Ingredient

    ingredients: list[Ingredient] = []
    idx = 0
    while True:
        ing_name = request.form.get(f"ing_name_{idx}", "").strip()
        if not ing_name and idx > 0:
            break
        if not ing_name:
            idx += 1
            # Check if there's a next row
            if not request.form.get(f"ing_name_{idx}", "").strip():
                break
            continue

        qty_raw = request.form.get(f"ing_qty_{idx}", "0").strip()
        try:
            qty = float(qty_raw)
        except ValueError:
            qty = 0.0

        unit = request.form.get(f"ing_unit_{idx}", "").strip()
        cat_raw = request.form.get(f"ing_category_{idx}", "other").strip()
        is_pantry = request.form.get(f"ing_pantry_{idx}") == "on"

        try:
            category = IngredientCategory(cat_raw)
        except ValueError:
            category = IngredientCategory.OTHER

        ingredients.append(
            Ingredient(
                ingredient=ing_name.lower(),
                quantity=qty,
                unit=parse_unit(unit),
                category=category,
                is_pantry_item=is_pantry,
            )
        )
        idx += 1
    return ingredients


def _register_shopping_list_routes(app: Flask) -> None:
    """Register shopping list page routes.

    Args:
        app: Flask application instance.
    """

    @app.route("/shopping-list")
    def shopping_list() -> str:
        """Render the shopping list page.

        Reads shopping list items from the session if available.

        Returns:
            Rendered shopping list HTML.
        """
        from flask import session

        raw_items = session.get("shopping_list_items")
        items: list[dict[str, object]] = (
            raw_items if isinstance(raw_items, list) else []
        )

        # Group items by category
        grouped: dict[str, list[dict[str, object]]] = {}
        for item in items:
            cat = str(item.get("category", "other"))
            if cat not in grouped:
                grouped[cat] = []
            grouped[cat].append(item)

        return render_template(
            "shopping_list.html",
            active_page="shopping_list",
            grouped_items=grouped,
            has_items=len(items) > 0,
        )

    @app.route("/shopping-list/generate", methods=["POST"])
    def shopping_list_generate() -> Response:
        """Generate a shopping list from meal names.

        Reads meal names from form input, runs the consolidation
        pipeline, and stores results in the session.

        Returns:
            Redirect to the shopping list page.
        """
        from flask import session

        meals_raw = request.form.get("meals", "").strip()

        if not meals_raw:
            flash("Please enter at least one meal.", "error")
            return redirect(url_for("shopping_list"))

        meal_names = [m.strip() for m in meals_raw.split("\n") if m.strip()]

        if not meal_names:
            flash("Please enter at least one meal.", "error")
            return redirect(url_for("shopping_list"))

        db_path = app.config["DATABASE_PATH"]
        recipe_store = RecipeStore(db_path)
        pantry_mgr = PantryManager(db_path)

        from grocery_butler.consolidator import Consolidator
        from grocery_butler.meal_parser import MealParser

        parser = MealParser(recipe_store)
        parsed_meals = parser.parse_meals(meal_names)

        consolidator = Consolidator()
        pantry_staples = recipe_store.get_pantry_staple_names()
        restock_queue = pantry_mgr.get_restock_queue()
        shopping_items = consolidator.consolidate_simple(
            parsed_meals, restock_queue, pantry_staples
        )

        session["shopping_list_items"] = [
            {
                "ingredient": item.ingredient,
                "quantity": item.quantity,
                "unit": item.unit,
                "category": item.category.value,
                "from_meals": item.from_meals,
            }
            for item in shopping_items
        ]

        flash(f"Generated shopping list from {len(meal_names)} meal(s).", "success")
        return redirect(url_for("shopping_list"))


def _register_pantry_routes(app: Flask) -> None:
    """Register pantry staples page routes.

    Args:
        app: Flask application instance.
    """

    @app.route("/pantry")
    def pantry() -> str:
        """Render the pantry staples page.

        Returns:
            Rendered pantry HTML.
        """
        db_path = app.config["DATABASE_PATH"]
        recipe_store = RecipeStore(db_path)
        staples = recipe_store.get_pantry_staples()
        categories = [cat.value for cat in IngredientCategory]

        return render_template(
            "pantry.html",
            active_page="pantry",
            staples=staples,
            categories=categories,
        )

    @app.route("/pantry/add", methods=["POST"])
    def pantry_add() -> Response:
        """Add a new pantry staple.

        Expects form fields: ``ingredient``, ``category``.

        Returns:
            Redirect to the pantry page.
        """
        ingredient = request.form.get("ingredient", "").strip()
        category = request.form.get("category", "other").strip()

        if not ingredient:
            flash("Ingredient name is required.", "error")
            return redirect(url_for("pantry"))

        db_path = app.config["DATABASE_PATH"]
        recipe_store = RecipeStore(db_path)

        try:
            recipe_store.add_pantry_staple(ingredient, category)
            display_name = ingredient.strip().title()
            flash(f"Added {display_name} to pantry staples.", "success")
        except sqlite3.IntegrityError:
            flash(f"{ingredient.title()} is already a pantry staple.", "error")

        return redirect(url_for("pantry"))

    @app.route("/pantry/<int:staple_id>/remove", methods=["POST"])
    def pantry_remove(staple_id: int) -> Response:
        """Remove a pantry staple.

        Args:
            staple_id: Database ID of the staple to remove.

        Returns:
            Redirect to the pantry page.
        """
        db_path = app.config["DATABASE_PATH"]
        recipe_store = RecipeStore(db_path)
        recipe_store.remove_pantry_staple(staple_id)
        flash("Pantry staple removed.", "success")
        return redirect(url_for("pantry"))


def _register_brand_routes(app: Flask) -> None:
    """Register brand preferences page routes.

    Args:
        app: Flask application instance.
    """

    @app.route("/brands")
    def brands() -> str:
        """Render the brand preferences page.

        Returns:
            Rendered brands HTML.
        """
        db_path = app.config["DATABASE_PATH"]
        recipe_store = RecipeStore(db_path)
        prefs = recipe_store.get_brand_preferences()

        # Group by preference type
        grouped: dict[str, list[BrandPreference]] = {
            "preferred": [],
            "avoid": [],
        }
        for pref in prefs:
            grouped[pref.preference_type.value].append(pref)

        return render_template(
            "brands.html",
            active_page="brands",
            grouped_prefs=grouped,
            match_types=[t.value for t in BrandMatchType],
            pref_types=[t.value for t in BrandPreferenceType],
        )

    @app.route("/brands/add", methods=["POST"])
    def brands_add() -> Response:
        """Add a new brand preference.

        Expects form fields: ``match_target``, ``match_type``,
        ``brand``, ``preference_type``, ``notes``.

        Returns:
            Redirect to the brands page.
        """
        match_target = request.form.get("match_target", "").strip()
        match_type = request.form.get("match_type", "ingredient").strip()
        brand = request.form.get("brand", "").strip()
        preference_type = request.form.get("preference_type", "preferred").strip()
        notes = request.form.get("notes", "").strip()

        if not match_target or not brand:
            flash("Target and brand name are required.", "error")
            return redirect(url_for("brands"))

        try:
            mt = BrandMatchType(match_type)
        except ValueError:
            flash(f"Invalid match type: {match_type}", "error")
            return redirect(url_for("brands"))

        try:
            pt = BrandPreferenceType(preference_type)
        except ValueError:
            flash(f"Invalid preference type: {preference_type}", "error")
            return redirect(url_for("brands"))

        pref = BrandPreference(
            match_target=match_target.lower(),
            match_type=mt,
            brand=brand,
            preference_type=pt,
            notes=notes,
        )

        db_path = app.config["DATABASE_PATH"]
        recipe_store = RecipeStore(db_path)

        try:
            recipe_store.add_brand_preference(pref)
            flash(f"Added brand preference for {brand}.", "success")
        except sqlite3.IntegrityError:
            flash(f"Brand preference for {brand} already exists.", "error")

        return redirect(url_for("brands"))

    @app.route("/brands/<int:pref_id>/remove", methods=["POST"])
    def brands_remove(pref_id: int) -> Response:
        """Remove a brand preference.

        Args:
            pref_id: Database ID of the preference to remove.

        Returns:
            Redirect to the brands page.
        """
        db_path = app.config["DATABASE_PATH"]
        recipe_store = RecipeStore(db_path)
        recipe_store.remove_brand_preference(pref_id)
        flash("Brand preference removed.", "success")
        return redirect(url_for("brands"))


def _register_preferences_routes(app: Flask) -> None:
    """Register user preferences page routes.

    Args:
        app: Flask application instance.
    """

    @app.route("/preferences", methods=["GET"])
    def preferences() -> str:
        """Render the preferences form page.

        Returns:
            Rendered preferences HTML.
        """
        db_path = app.config["DATABASE_PATH"]
        recipe_store = RecipeStore(db_path)
        all_prefs = recipe_store.get_all_preferences()

        return render_template(
            "preferences.html",
            active_page="preferences",
            prefs=all_prefs,
        )

    @app.route("/preferences", methods=["POST"])
    def preferences_save() -> Response:
        """Save all preference values from the form.

        Returns:
            Redirect to the preferences page with flash message.
        """
        db_path = app.config["DATABASE_PATH"]
        recipe_store = RecipeStore(db_path)

        known_keys = [
            "default_servings",
            "default_units",
            "dietary_restrictions",
        ]

        for key in known_keys:
            value = request.form.get(key, "").strip()
            if value:
                recipe_store.set_preference(key, value)

        flash("Preferences saved.", "success")
        return redirect(url_for("preferences"))
