"""Product search and mapping cache for Safeway integration.

Searches the Nimbus API for grocery products, caches results in the
``product_mapping`` SQLite table, and returns :class:`SafewayProduct`
instances.  Pinned mappings bypass the API entirely.

Cache strategy:
- ``get_cached_product`` returns a pinned or non-stale cached mapping
  (touching ``times_selected``/``last_used``) without ever performing a
  live search; a stale or absent mapping returns ``None`` so the caller
  falls back to a fresh search and selection.
- ``save_mapping``/``pin_mapping`` persist the *selected* product (name,
  price, size, and stock status) so the cache reflects what the user
  actually chose, not merely the top raw search hit.
- ``reverify_product`` re-runs a live search for a cache hit's
  ingredient description and reconciles the cached ``product_id``
  against the fresh candidates, returning up-to-date price/size/stock
  (or marking the product out-of-stock if it no longer appears, so the
  substitution flow can trigger). A search failure falls back to the
  cached product unchanged.
- Mappings older than ``CACHE_MAX_AGE_DAYS`` are considered stale and
  re-fetched on the next search.
- Pinned mappings never expire.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from grocery_butler.db.adapter import DatabaseConnection, DictRow

from grocery_butler.db import get_connection, init_db
from grocery_butler.models import SafewayProduct
from grocery_butler.safeway_client import SafewayAPIError, SafewayClient

logger = logging.getLogger(__name__)

# Nimbus search path template
_SEARCH_PATH = "/api/v2/grocerystore/search"
_DEFAULT_ROWS = 10

# Cache entries older than this are considered stale (unless pinned)
CACHE_MAX_AGE_DAYS = 7


class ProductSearchError(Exception):
    """Raised when a product search fails."""


@dataclass
class CachedMapping:
    """A product mapping row from the database.

    Attributes:
        mapping_id: Primary key in the product_mapping table.
        ingredient_description: The search term that produced this mapping.
        product: The cached Safeway product.
        is_pinned: Whether the user has pinned this mapping.
        times_selected: Number of times this mapping has been used.
        last_used: When the mapping was last used.
    """

    mapping_id: int
    ingredient_description: str
    product: SafewayProduct
    is_pinned: bool
    times_selected: int
    last_used: datetime


class ProductSearchService:
    """Search Safeway products and manage the mapping cache.

    Args:
        safeway_client: Authenticated Safeway API client.
        db_path: Path to the SQLite database.
    """

    def __init__(
        self,
        safeway_client: SafewayClient,
        db_path: str,
    ) -> None:
        """Initialize the product search service.

        Args:
            safeway_client: Authenticated Safeway API client.
            db_path: Path to the SQLite database.
        """
        self._client = safeway_client
        self._db_path = db_path
        init_db(db_path)

    def _connect(self) -> DatabaseConnection:
        """Create a new database connection.

        Returns:
            Configured DatabaseConnection.
        """
        return get_connection(self._db_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search_products(
        self,
        query: str,
        rows: int = _DEFAULT_ROWS,
    ) -> list[SafewayProduct]:
        """Search Safeway for products matching *query*.

        Args:
            query: Search term (e.g. ``"whole milk"``).
            rows: Maximum number of results to return.

        Returns:
            List of parsed SafewayProduct instances.

        Raises:
            ProductSearchError: If the API call fails.
        """
        try:
            data = self._client.get(
                _SEARCH_PATH,
                params={
                    "q": query,
                    "storeId": self._client.store_id,
                    "rows": str(rows),
                },
            )
        except SafewayAPIError as exc:
            raise ProductSearchError(
                f"Product search failed for '{query}': {exc}"
            ) from exc

        return _parse_search_results(data)

    def get_cached_product(
        self,
        search_term: str,
    ) -> CachedMapping | None:
        """Return a usable cached mapping for *search_term*, if any.

        A pinned mapping is always usable; a non-pinned mapping is
        usable if it is not yet stale (see ``CACHE_MAX_AGE_DAYS``). In
        either case, the mapping is "touched" (``times_selected``
        incremented and ``last_used`` refreshed) before being returned.

        This never performs a live search; callers should fall back to
        :meth:`search_products` (and, for cache hits, re-verify via
        :meth:`reverify_product`) when this returns ``None``.

        Args:
            search_term: The ingredient search term.

        Returns:
            The usable cached mapping, or ``None`` if no cached mapping
            exists or the only one found is stale and not pinned.
        """
        cached = self.get_cached_mapping(search_term)
        if cached is None:
            return None
        if not (cached.is_pinned or not self._is_stale(cached)):
            return None

        self._touch_mapping(cached.mapping_id)
        return cached

    def reverify_product(self, cached: CachedMapping) -> SafewayProduct:
        """Re-verify a cached product against a fresh live search.

        Cached price/size/stock can go stale between visits, so a cache
        hit re-runs a live search for the mapping's ingredient
        description and looks for a fresh candidate whose
        ``product_id`` matches the cached product. When found, the
        fresh candidate (with current price, size, and stock) is
        returned. When no fresh candidate matches -- the product has
        been delisted or is otherwise absent from results -- the cached
        product is returned with ``in_stock`` forced to ``False`` so
        the substitution flow can trigger. If the live search itself
        fails, the cached product is returned unchanged (a transient
        API outage should not be treated as an out-of-stock signal).

        Args:
            cached: The cached mapping to re-verify.

        Returns:
            The re-verified SafewayProduct.
        """
        try:
            fresh = self.search_products(cached.ingredient_description)
        except ProductSearchError:
            logger.warning(
                "Re-verification search failed for '%s'; using cached product",
                cached.ingredient_description,
            )
            return cached.product

        for candidate in fresh:
            if candidate.product_id == cached.product.product_id:
                return candidate

        return cached.product.model_copy(update={"in_stock": False})

    # ------------------------------------------------------------------
    # Cache operations
    # ------------------------------------------------------------------

    def get_cached_mapping(
        self,
        search_term: str,
    ) -> CachedMapping | None:
        """Look up a cached product mapping by search term.

        Args:
            search_term: The ingredient description to look up.

        Returns:
            The cached mapping, or None if not found.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, ingredient_description, safeway_product_id,"
                " safeway_product_name, safeway_price, safeway_product_size,"
                " safeway_in_stock, last_used,"
                " times_selected, is_pinned"
                " FROM product_mapping"
                " WHERE ingredient_description = ?"
                " ORDER BY is_pinned DESC, times_selected DESC"
                " LIMIT 1",
                (search_term,),
            ).fetchone()
            if row is None:
                return None
            return _row_to_cached_mapping(row)
        finally:
            conn.close()

    def save_mapping(
        self,
        search_term: str,
        product: SafewayProduct,
    ) -> int:
        """Insert or update a product mapping in the cache.

        If a mapping already exists for this search term and product,
        increments ``times_selected`` and updates ``last_used``.
        Otherwise inserts a new row.

        Args:
            search_term: The ingredient description.
            product: The Safeway product to cache.

        Returns:
            The mapping row ID.
        """
        conn = self._connect()
        try:
            existing = conn.execute(
                "SELECT id FROM product_mapping"
                " WHERE ingredient_description = ?"
                " AND safeway_product_id = ?",
                (search_term, product.product_id),
            ).fetchone()

            if existing:
                row_id = existing["id"]
                conn.execute(
                    "UPDATE product_mapping"
                    " SET times_selected = times_selected + 1,"
                    " last_used = CURRENT_TIMESTAMP,"
                    " safeway_price = ?,"
                    " safeway_product_size = ?,"
                    " safeway_in_stock = ?"
                    " WHERE id = ?",
                    (product.price, product.size, product.in_stock, row_id),
                )
            else:
                cursor = conn.execute(
                    "INSERT INTO product_mapping"
                    " (ingredient_description, safeway_product_id,"
                    "  safeway_product_name, safeway_price,"
                    "  safeway_product_size, safeway_in_stock)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        search_term,
                        product.product_id,
                        product.name,
                        product.price,
                        product.size,
                        product.in_stock,
                    ),
                )
                row_id = cursor.lastrowid
                if row_id is None:  # pragma: no cover
                    msg = "INSERT did not return a row ID"
                    raise RuntimeError(msg)

            conn.commit()
            return int(row_id)
        finally:
            conn.close()

    def pin_mapping(
        self,
        search_term: str,
        product: SafewayProduct,
    ) -> int:
        """Pin a product mapping so it always bypasses search.

        Unpins any other mapping for the same search term first.

        Args:
            search_term: The ingredient description.
            product: The product to pin.

        Returns:
            The pinned mapping row ID.
        """
        conn = self._connect()
        try:
            # Unpin existing mappings for this search term
            conn.execute(
                "UPDATE product_mapping SET is_pinned = FALSE"
                " WHERE ingredient_description = ?",
                (search_term,),
            )
            # Upsert the pinned mapping
            existing = conn.execute(
                "SELECT id FROM product_mapping"
                " WHERE ingredient_description = ?"
                " AND safeway_product_id = ?",
                (search_term, product.product_id),
            ).fetchone()

            if existing:
                row_id = existing["id"]
                conn.execute(
                    "UPDATE product_mapping"
                    " SET is_pinned = TRUE,"
                    " last_used = CURRENT_TIMESTAMP,"
                    " safeway_price = ?,"
                    " safeway_product_size = ?,"
                    " safeway_in_stock = ?"
                    " WHERE id = ?",
                    (product.price, product.size, product.in_stock, row_id),
                )
            else:
                cursor = conn.execute(
                    "INSERT INTO product_mapping"
                    " (ingredient_description, safeway_product_id,"
                    "  safeway_product_name, safeway_price,"
                    "  safeway_product_size, safeway_in_stock, is_pinned)"
                    " VALUES (?, ?, ?, ?, ?, ?, TRUE)",
                    (
                        search_term,
                        product.product_id,
                        product.name,
                        product.price,
                        product.size,
                        product.in_stock,
                    ),
                )
                row_id = cursor.lastrowid
                if row_id is None:  # pragma: no cover
                    msg = "INSERT did not return a row ID"
                    raise RuntimeError(msg)

            conn.commit()
            return int(row_id)
        finally:
            conn.close()

    def unpin_mapping(self, search_term: str) -> bool:
        """Remove the pin from a mapping.

        Args:
            search_term: The ingredient description to unpin.

        Returns:
            True if a pinned mapping was found and unpinned.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE product_mapping SET is_pinned = FALSE"
                " WHERE ingredient_description = ? AND is_pinned = TRUE",
                (search_term,),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def delete_mapping(self, search_term: str) -> int:
        """Delete all cached mappings for a search term.

        Args:
            search_term: The ingredient description to remove.

        Returns:
            Number of rows deleted.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "DELETE FROM product_mapping WHERE ingredient_description = ?",
                (search_term,),
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _touch_mapping(self, mapping_id: int) -> None:
        """Update last_used and increment times_selected for a mapping.

        Args:
            mapping_id: The row ID to update.
        """
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE product_mapping"
                " SET times_selected = times_selected + 1,"
                " last_used = CURRENT_TIMESTAMP"
                " WHERE id = ?",
                (mapping_id,),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _is_stale(cached: CachedMapping) -> bool:
        """Check if a cached mapping has expired.

        Args:
            cached: The cached mapping to check.

        Returns:
            True if the mapping is older than CACHE_MAX_AGE_DAYS.
        """
        cutoff = datetime.now(tz=UTC) - timedelta(
            days=CACHE_MAX_AGE_DAYS,
        )
        return cached.last_used < cutoff


# ------------------------------------------------------------------
# Pure helper functions
# ------------------------------------------------------------------


def _parse_search_results(data: dict[str, Any]) -> list[SafewayProduct]:
    """Parse Nimbus search API response into SafewayProduct models.

    The Nimbus response has a ``productsInfo`` list, each entry
    containing product details.

    Args:
        data: Raw JSON response from the Nimbus search endpoint.

    Returns:
        List of parsed SafewayProduct instances.
    """
    products_info = data.get("productsInfo", [])
    results: list[SafewayProduct] = []
    for item in products_info:
        product = _parse_single_product(item)
        if product is not None:
            results.append(product)
    return results


def _parse_single_product(item: dict[str, Any]) -> SafewayProduct | None:
    """Parse a single product entry from the Nimbus response.

    Args:
        item: A single product dict from the ``productsInfo`` list.

    Returns:
        A SafewayProduct, or None if required fields are missing.
    """
    product_id = item.get("upc", "") or item.get("pid", "")
    name = item.get("name", "")
    if not product_id or not name:
        return None

    price = _parse_price(item)
    return SafewayProduct(
        product_id=str(product_id),
        name=str(name),
        price=price,
        unit_price=_safe_float(item.get("unitPrice")),
        size=str(item.get("size", "")),
        in_stock=item.get("inStock", True) is not False,
    )


def _parse_price(item: dict[str, Any]) -> float:
    """Extract the best available price from a product entry.

    Prefers ``salePrice`` over ``price`` over ``basePrice``.

    Args:
        item: Product dict from the Nimbus response.

    Returns:
        Price as a float, defaulting to 0.0 if unparseable.
    """
    for key in ("salePrice", "price", "basePrice"):
        value = _safe_float(item.get(key))
        if value is not None and value > 0:
            return value
    return 0.0


def _safe_float(value: float | int | str | None) -> float | None:
    """Safely convert a value to float.

    Args:
        value: Numeric or string value that might be convertible to float.

    Returns:
        Float value, or None if conversion fails.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_to_cached_mapping(row: DictRow) -> CachedMapping:
    """Convert a database row to a CachedMapping.

    Args:
        row: A dict-like row from the product_mapping table.

    Returns:
        Populated CachedMapping instance.
    """
    last_used_str = row["last_used"]
    if isinstance(last_used_str, str):
        last_used = datetime.fromisoformat(last_used_str).replace(
            tzinfo=UTC,
        )
    else:
        last_used = datetime.now(tz=UTC)

    in_stock_value = row["safeway_in_stock"]
    return CachedMapping(
        mapping_id=row["id"],
        ingredient_description=row["ingredient_description"],
        product=SafewayProduct(
            product_id=row["safeway_product_id"],
            name=row["safeway_product_name"],
            price=row["safeway_price"] or 0.0,
            size=row["safeway_product_size"] or "",
            in_stock=True if in_stock_value is None else bool(in_stock_value),
        ),
        is_pinned=bool(row["is_pinned"]),
        times_selected=row["times_selected"],
        last_used=last_used,
    )
