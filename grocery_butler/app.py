"""Flask web application for Grocery Butler dashboard.

Provides a web interface for managing household inventory, viewing
recipes, and monitoring the restock queue. Uses SQLite with WAL mode
for concurrent read access.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import TYPE_CHECKING

from flask import Flask, flash, g, jsonify, redirect, render_template, request, url_for

if TYPE_CHECKING:
    from werkzeug.wrappers import Response

from grocery_butler.db import get_connection, init_db
from grocery_butler.models import IngredientCategory, InventoryItem, InventoryStatus
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
    app.config["SECRET_KEY"] = os.environ.get(
        "FLASK_SECRET_KEY", os.urandom(32).hex()
    )

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
    _register_placeholder_routes(app)


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


def _register_placeholder_routes(app: Flask) -> None:
    """Register placeholder routes for unimplemented pages.

    Args:
        app: Flask application instance.
    """

    @app.route("/recipes")
    def recipes() -> str:
        """Render the recipes placeholder page.

        Returns:
            Rendered placeholder HTML.
        """
        return render_template(
            "placeholder.html",
            active_page="recipes",
            page_title="Recipes",
        )

    @app.route("/pantry")
    def pantry() -> str:
        """Render the pantry placeholder page.

        Returns:
            Rendered placeholder HTML.
        """
        return render_template(
            "placeholder.html",
            active_page="pantry",
            page_title="Pantry Staples",
        )

    @app.route("/shopping-list")
    def shopping_list() -> str:
        """Render the shopping list placeholder page.

        Returns:
            Rendered placeholder HTML.
        """
        return render_template(
            "placeholder.html",
            active_page="shopping_list",
            page_title="Shopping List",
        )

    @app.route("/brands")
    def brands() -> str:
        """Render the brands placeholder page.

        Returns:
            Rendered placeholder HTML.
        """
        return render_template(
            "placeholder.html",
            active_page="brands",
            page_title="Brand Preferences",
        )

    @app.route("/preferences")
    def preferences() -> str:
        """Render the preferences placeholder page.

        Returns:
            Rendered placeholder HTML.
        """
        return render_template(
            "placeholder.html",
            active_page="preferences",
            page_title="Preferences",
        )
