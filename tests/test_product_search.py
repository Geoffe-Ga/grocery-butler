"""Tests for grocery_butler.product_search module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from grocery_butler.models import SafewayProduct
from grocery_butler.product_search import (
    CACHE_MAX_AGE_DAYS,
    CachedMapping,
    ProductSearchError,
    ProductSearchService,
    _parse_price,
    _parse_search_results,
    _parse_single_product,
    _row_to_cached_mapping,
    _safe_float,
)
from grocery_butler.safeway_client import SafewayClient

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


def _make_nimbus_product(
    upc: str = "00012345",
    name: str = "Whole Milk 1 Gallon",
    price: float = 4.99,
    size: str = "1 gal",
    in_stock: bool = True,
) -> dict[str, Any]:
    """Build a product dict matching Nimbus API format."""
    return {
        "upc": upc,
        "name": name,
        "price": price,
        "size": size,
        "inStock": in_stock,
    }


def _make_search_response(
    products: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a Nimbus search response."""
    return {"productsInfo": products or []}


class _MockTransport(httpx.BaseTransport):
    """Mock transport that returns pre-configured responses."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        """Initialize with a list of responses to return in order.

        Args:
            responses: Ordered list of responses.
        """
        self._responses = list(responses)
        self._index = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Return the next pre-configured response.

        Args:
            request: The outgoing HTTP request.

        Returns:
            The next mock response.
        """
        if self._index >= len(self._responses):
            return httpx.Response(500, json={"error": "no more mock responses"})
        resp = self._responses[self._index]
        self._index += 1
        return resp


def _make_authn_response(session_token: str = "test-session") -> httpx.Response:
    """Build a mock Okta authn response."""
    return httpx.Response(200, json={"sessionToken": session_token})


def _make_authorize_redirect(
    access_token: str = "test-access-token",
) -> httpx.Response:
    """Build a mock OAuth2 redirect with token in fragment."""
    location = f"https://www.safeway.com#access_token={access_token}&expires_in=3600"
    return httpx.Response(302, headers={"location": location})


def _make_authenticated_client(
    api_responses: list[httpx.Response],
) -> tuple[SafewayClient, _MockTransport]:
    """Create a SafewayClient pre-authenticated with mock transport.

    Args:
        api_responses: Responses for API calls after auth.

    Returns:
        Tuple of (client, transport).
    """
    all_responses = [
        _make_authn_response(),
        _make_authorize_redirect(),
        *api_responses,
    ]
    transport = _MockTransport(all_responses)
    http = httpx.Client(transport=transport)
    client = SafewayClient("user", "pass", "1234", http_client=http)
    return client, transport


@pytest.fixture
def db_path(tmp_path: Any) -> str:
    """Provide a temporary database path.

    Args:
        tmp_path: Pytest tmp_path fixture.

    Returns:
        Path string for the test database.
    """
    return str(tmp_path / "test.db")


# ------------------------------------------------------------------
# Tests: _parse_search_results
# ------------------------------------------------------------------


class TestParseSearchResults:
    """Tests for _parse_search_results."""

    def test_empty_response(self) -> None:
        """Test parsing empty search results."""
        result = _parse_search_results({})
        assert result == []

    def test_empty_products_list(self) -> None:
        """Test parsing response with empty productsInfo."""
        result = _parse_search_results({"productsInfo": []})
        assert result == []

    def test_single_product(self) -> None:
        """Test parsing a single product."""
        data = _make_search_response([_make_nimbus_product()])
        result = _parse_search_results(data)

        assert len(result) == 1
        assert result[0].product_id == "00012345"
        assert result[0].name == "Whole Milk 1 Gallon"
        assert result[0].price == Decimal("4.99")
        assert result[0].size == "1 gal"
        assert result[0].in_stock is True

    def test_multiple_products(self) -> None:
        """Test parsing multiple products."""
        data = _make_search_response(
            [
                _make_nimbus_product(upc="001", name="Milk A"),
                _make_nimbus_product(upc="002", name="Milk B"),
            ]
        )
        result = _parse_search_results(data)
        assert len(result) == 2

    def test_skips_products_without_id(self) -> None:
        """Test that products without UPC or PID are skipped."""
        data = _make_search_response(
            [
                {"name": "No ID Product", "price": 1.0, "size": "1"},
            ]
        )
        result = _parse_search_results(data)
        assert result == []

    def test_skips_products_without_name(self) -> None:
        """Test that products without a name are skipped."""
        data = _make_search_response(
            [
                {"upc": "123", "price": 1.0, "size": "1"},
            ]
        )
        result = _parse_search_results(data)
        assert result == []


# ------------------------------------------------------------------
# Tests: _parse_single_product
# ------------------------------------------------------------------


class TestParseSingleProduct:
    """Tests for _parse_single_product."""

    def test_uses_pid_when_no_upc(self) -> None:
        """Test fallback to PID when UPC is missing."""
        item = {"pid": "PID123", "name": "Test", "price": 1.0, "size": "1"}
        result = _parse_single_product(item)

        assert result is not None
        assert result.product_id == "PID123"

    def test_out_of_stock(self) -> None:
        """Test parsing an out-of-stock product."""
        item = _make_nimbus_product(in_stock=False)
        result = _parse_single_product(item)

        assert result is not None
        assert result.in_stock is False

    def test_sale_price_preferred(self) -> None:
        """Test that salePrice is preferred over regular price."""
        item = {
            "upc": "123",
            "name": "On Sale",
            "salePrice": 2.99,
            "price": 4.99,
            "size": "1",
        }
        result = _parse_single_product(item)

        assert result is not None
        assert result.price == Decimal("2.99")

    def test_unit_price_parsed(self) -> None:
        """Test that unitPrice is parsed when present."""
        item = {
            "upc": "123",
            "name": "Test",
            "price": 4.99,
            "unitPrice": 0.16,
            "size": "32 oz",
        }
        result = _parse_single_product(item)

        assert result is not None
        assert result.unit_price == Decimal("0.16")


# ------------------------------------------------------------------
# Tests: _parse_price
# ------------------------------------------------------------------


class TestParsePrice:
    """Tests for _parse_price."""

    def test_sale_price_first(self) -> None:
        """Test salePrice is preferred."""
        assert _parse_price({"salePrice": 2.0, "price": 3.0}) == 2.0

    def test_regular_price_fallback(self) -> None:
        """Test regular price when no sale price."""
        assert _parse_price({"price": 3.0}) == 3.0

    def test_base_price_fallback(self) -> None:
        """Test basePrice as last resort."""
        assert _parse_price({"basePrice": 5.0}) == 5.0

    def test_no_price_returns_zero(self) -> None:
        """Test default to 0.0 when no price found."""
        assert _parse_price({}) == 0.0

    def test_skips_zero_prices(self) -> None:
        """Test that zero prices are skipped."""
        assert _parse_price({"salePrice": 0, "price": 3.0}) == 3.0


# ------------------------------------------------------------------
# Tests: _safe_float
# ------------------------------------------------------------------


class TestSafeFloat:
    """Tests for _safe_float."""

    def test_none_returns_none(self) -> None:
        """Test None input."""
        assert _safe_float(None) is None

    def test_valid_float(self) -> None:
        """Test valid float conversion."""
        assert _safe_float(3.14) == 3.14

    def test_valid_int(self) -> None:
        """Test int to float conversion."""
        assert _safe_float(5) == 5.0

    def test_valid_string(self) -> None:
        """Test numeric string conversion."""
        assert _safe_float("2.99") == 2.99

    def test_invalid_string(self) -> None:
        """Test non-numeric string returns None."""
        assert _safe_float("not a number") is None


# ------------------------------------------------------------------
# Tests: ProductSearchService.search_products
# ------------------------------------------------------------------


class TestSearchProducts:
    """Tests for ProductSearchService.search_products."""

    @patch("grocery_butler.safeway_client.time.sleep")
    def test_search_returns_products(self, mock_sleep: object, db_path: str) -> None:
        """Test successful product search."""
        response_data = _make_search_response(
            [
                _make_nimbus_product(upc="001", name="Milk A", price=3.99),
            ]
        )
        client, _transport = _make_authenticated_client(
            [
                httpx.Response(200, json=response_data),
            ]
        )
        service = ProductSearchService(client, db_path)

        results = service.search_products("milk")

        assert len(results) == 1
        assert results[0].product_id == "001"
        assert results[0].price == Decimal("3.99")
        client.close()

    @patch("grocery_butler.safeway_client.time.sleep")
    def test_search_empty_results(self, mock_sleep: object, db_path: str) -> None:
        """Test search returning no products."""
        client, _transport = _make_authenticated_client(
            [
                httpx.Response(200, json=_make_search_response([])),
            ]
        )
        service = ProductSearchService(client, db_path)

        results = service.search_products("nonexistent item")

        assert results == []
        client.close()

    @patch("grocery_butler.safeway_client.time.sleep")
    def test_search_api_error_raises(self, mock_sleep: object, db_path: str) -> None:
        """Test that API errors are wrapped in ProductSearchError."""
        client, _transport = _make_authenticated_client(
            [
                httpx.Response(500, json={"error": "server error"}),
                # Retry after re-auth also fails
                _make_authn_response(),
                _make_authorize_redirect(),
                httpx.Response(500, json={"error": "still broken"}),
            ]
        )
        service = ProductSearchService(client, db_path)

        with pytest.raises(ProductSearchError, match="Product search failed"):
            service.search_products("milk")
        client.close()


# ------------------------------------------------------------------
# Tests: Cache operations
# ------------------------------------------------------------------


class TestCacheOperations:
    """Tests for product mapping cache CRUD."""

    def _make_service(self, db_path: str) -> ProductSearchService:
        """Create a service with a dummy client for cache-only tests.

        Args:
            db_path: Database path.

        Returns:
            ProductSearchService instance.
        """
        transport = _MockTransport([])
        http = httpx.Client(transport=transport)
        client = SafewayClient("user", "pass", "1234", http_client=http)
        return ProductSearchService(client, db_path)

    def _sample_product(self) -> SafewayProduct:
        """Create a sample SafewayProduct for testing.

        Returns:
            A SafewayProduct instance.
        """
        return SafewayProduct(
            product_id="UPC001",
            name="Organic Whole Milk",
            price=5.99,
            size="1 gal",
        )

    def test_save_and_retrieve_mapping(self, db_path: str) -> None:
        """Test saving and retrieving a product mapping."""
        service = self._make_service(db_path)
        product = self._sample_product()

        row_id = service.save_mapping("whole milk", product)
        cached = service.get_cached_mapping("whole milk")

        assert row_id > 0
        assert cached is not None
        assert cached.product.product_id == "UPC001"
        assert cached.product.name == "Organic Whole Milk"
        assert cached.product.price == Decimal("5.99")
        assert cached.is_pinned is False
        assert cached.times_selected == 1

    def test_save_existing_increments_count(self, db_path: str) -> None:
        """Test that saving same mapping again increments times_selected."""
        service = self._make_service(db_path)
        product = self._sample_product()

        service.save_mapping("whole milk", product)
        service.save_mapping("whole milk", product)
        cached = service.get_cached_mapping("whole milk")

        assert cached is not None
        assert cached.times_selected == 2

    def test_get_nonexistent_mapping(self, db_path: str) -> None:
        """Test looking up a mapping that doesn't exist."""
        service = self._make_service(db_path)
        assert service.get_cached_mapping("not cached") is None

    def test_pin_mapping(self, db_path: str) -> None:
        """Test pinning a product mapping."""
        service = self._make_service(db_path)
        product = self._sample_product()

        row_id = service.pin_mapping("whole milk", product)
        cached = service.get_cached_mapping("whole milk")

        assert row_id > 0
        assert cached is not None
        assert cached.is_pinned is True

    def test_pin_replaces_existing_pin(self, db_path: str) -> None:
        """Test that pinning unpins the previous mapping."""
        service = self._make_service(db_path)
        product_a = SafewayProduct(
            product_id="A", name="Brand A Milk", price=4.99, size="1 gal"
        )
        product_b = SafewayProduct(
            product_id="B", name="Brand B Milk", price=5.49, size="1 gal"
        )

        service.pin_mapping("milk", product_a)
        service.pin_mapping("milk", product_b)
        cached = service.get_cached_mapping("milk")

        assert cached is not None
        assert cached.product.product_id == "B"
        assert cached.is_pinned is True

    def test_unpin_mapping(self, db_path: str) -> None:
        """Test unpinning a mapping."""
        service = self._make_service(db_path)
        product = self._sample_product()

        service.pin_mapping("milk", product)
        result = service.unpin_mapping("milk")
        cached = service.get_cached_mapping("milk")

        assert result is True
        assert cached is not None
        assert cached.is_pinned is False

    def test_unpin_nonexistent_returns_false(self, db_path: str) -> None:
        """Test unpinning when no pinned mapping exists."""
        service = self._make_service(db_path)
        assert service.unpin_mapping("nothing") is False

    def test_delete_mapping(self, db_path: str) -> None:
        """Test deleting a mapping."""
        service = self._make_service(db_path)
        product = self._sample_product()

        service.save_mapping("milk", product)
        count = service.delete_mapping("milk")

        assert count == 1
        assert service.get_cached_mapping("milk") is None

    def test_delete_nonexistent_returns_zero(self, db_path: str) -> None:
        """Test deleting a mapping that doesn't exist."""
        service = self._make_service(db_path)
        assert service.delete_mapping("nothing") == 0

    def test_touch_mapping_updates_count(self, db_path: str) -> None:
        """Test _touch_mapping increments times_selected."""
        service = self._make_service(db_path)
        product = self._sample_product()

        row_id = service.save_mapping("milk", product)
        service._touch_mapping(row_id)
        cached = service.get_cached_mapping("milk")

        assert cached is not None
        assert cached.times_selected == 2

    # ------------------------------------------------------------------
    # Regression guards for issue #71: cached rows must persist the real
    # product size and stock status instead of being rehydrated with a
    # hardcoded size="" and a default in_stock=True. Those defaults used
    # to pin quantity calculations to 1 (unparseable_size) and prevented
    # the substitution flow from ever triggering for cached items.
    # ------------------------------------------------------------------

    def test_save_persists_size(self, db_path: str) -> None:
        """Test save_mapping persists the product's real size."""
        service = self._make_service(db_path)
        product = SafewayProduct(
            product_id="UPC010", name="Whole Milk", price=4.99, size="1 gal"
        )

        service.save_mapping("whole milk", product)
        cached = service.get_cached_mapping("whole milk")

        assert cached is not None
        assert cached.product.size == "1 gal"

    def test_save_persists_stock(self, db_path: str) -> None:
        """Test save_mapping persists an out-of-stock product's flag."""
        service = self._make_service(db_path)
        product = SafewayProduct(
            product_id="UPC011",
            name="Sold Out Milk",
            price=4.99,
            size="1 gal",
            in_stock=False,
        )

        service.save_mapping("whole milk", product)
        cached = service.get_cached_mapping("whole milk")

        assert cached is not None
        assert cached.product.in_stock is False

    def test_pin_persists_size_and_stock(self, db_path: str) -> None:
        """Test pin_mapping persists size and in_stock like save_mapping."""
        service = self._make_service(db_path)
        product = SafewayProduct(
            product_id="UPC012",
            name="Pinned Milk",
            price=5.49,
            size="2 gal",
            in_stock=False,
        )

        service.pin_mapping("whole milk", product)
        cached = service.get_cached_mapping("whole milk")

        assert cached is not None
        assert cached.product.size == "2 gal"
        assert cached.product.in_stock is False

    def test_save_update_path_reflects_latest_size_stock_price(
        self, db_path: str
    ) -> None:
        """Test saving the same product twice updates size/stock/price.

        The UPDATE branch of save_mapping (same search term + product_id
        already cached) must refresh size and in_stock alongside price,
        not just price.
        """
        service = self._make_service(db_path)
        original = SafewayProduct(
            product_id="UPC013",
            name="Whole Milk",
            price=4.99,
            size="1 gal",
            in_stock=True,
        )
        updated = SafewayProduct(
            product_id="UPC013",
            name="Whole Milk",
            price=5.49,
            size="0.5 gal",
            in_stock=False,
        )

        service.save_mapping("whole milk", original)
        service.save_mapping("whole milk", updated)
        cached = service.get_cached_mapping("whole milk")

        assert cached is not None
        assert cached.product.price == Decimal("5.49")
        assert cached.product.size == "0.5 gal"
        assert cached.product.in_stock is False

    def test_pin_update_path_reflects_latest_size_stock_price(
        self, db_path: str
    ) -> None:
        """Test re-pinning an already-cached product refreshes size/stock/price.

        The UPDATE branch of pin_mapping (same search term + product_id
        already cached) must refresh size and in_stock alongside price,
        mirroring the save_mapping update path.
        """
        service = self._make_service(db_path)
        original = SafewayProduct(
            product_id="UPC014",
            name="Whole Milk",
            price=4.99,
            size="1 gal",
            in_stock=True,
        )
        updated = SafewayProduct(
            product_id="UPC014",
            name="Whole Milk",
            price=5.79,
            size="0.5 gal",
            in_stock=False,
        )

        service.save_mapping("whole milk", original)
        service.pin_mapping("whole milk", updated)
        cached = service.get_cached_mapping("whole milk")

        assert cached is not None
        assert cached.is_pinned is True
        assert cached.product.price == Decimal("5.79")
        assert cached.product.size == "0.5 gal"
        assert cached.product.in_stock is False

    def test_legacy_row_without_size_stock_columns_rehydrates_defaults(
        self, db_path: str
    ) -> None:
        """Test rows lacking real size/stock data rehydrate to safe defaults.

        Regression guard for issue #71: rows written before migration 007
        have NULL ``safeway_product_size``/``safeway_in_stock``. Those
        must rehydrate to ``size == ""`` and ``in_stock is True`` rather
        than raising.
        """
        service = self._make_service(db_path)
        conn = service._connect()
        try:
            conn.execute(
                "INSERT INTO product_mapping"
                " (ingredient_description, safeway_product_id,"
                "  safeway_product_name, safeway_price)"
                " VALUES (?, ?, ?, ?)",
                ("legacy item", "LEGACY1", "Legacy Product", 2.99),
            )
            conn.commit()
        finally:
            conn.close()

        cached = service.get_cached_mapping("legacy item")

        assert cached is not None
        assert cached.product.size == ""
        assert cached.product.in_stock is True


# ------------------------------------------------------------------
# Tests: get_cached_product
# ------------------------------------------------------------------


class TestGetCachedProduct:
    """Tests for ProductSearchService.get_cached_product (issue #71).

    Replaces the cache-hit half of the removed ``search_or_cached``: a
    pinned or fresh mapping is returned (touching times_selected /
    last_used) without ever performing a live search; a stale or absent
    mapping returns None so the caller can fall back to a fresh search.
    """

    def _make_service(self, db_path: str) -> ProductSearchService:
        """Create a service with a dummy client for cache-only tests.

        Args:
            db_path: Database path.

        Returns:
            ProductSearchService instance.
        """
        transport = _MockTransport([])
        http = httpx.Client(transport=transport)
        client = SafewayClient("user", "pass", "1234", http_client=http)
        return ProductSearchService(client, db_path)

    def test_returns_pinned_without_search(self, db_path: str) -> None:
        """Test pinned mappings are returned without calling search_products."""
        service = self._make_service(db_path)
        product = SafewayProduct(
            product_id="PIN1", name="Pinned Milk", price=4.99, size="1 gal"
        )
        service.pin_mapping("milk", product)

        with patch.object(service, "search_products") as mock_search:
            result = service.get_cached_product("milk")

        mock_search.assert_not_called()
        assert result is not None
        assert result.product.product_id == "PIN1"

    def test_returns_fresh_cache(self, db_path: str) -> None:
        """Test a fresh (non-stale, non-pinned) mapping is returned."""
        service = self._make_service(db_path)
        product = SafewayProduct(
            product_id="CACHE1", name="Cached Milk", price=4.99, size="1 gal"
        )
        service.save_mapping("milk", product)

        result = service.get_cached_product("milk")

        assert result is not None
        assert result.product.product_id == "CACHE1"

    def test_absent_returns_none(self, db_path: str) -> None:
        """Test an uncached search term returns None."""
        service = self._make_service(db_path)
        assert service.get_cached_product("not cached") is None

    def test_stale_returns_none(self, db_path: str) -> None:
        """Test a stale, unpinned mapping is not returned."""
        service = self._make_service(db_path)
        product = SafewayProduct(
            product_id="OLD1", name="Old Milk", price=3.99, size="1 gal"
        )
        service.save_mapping("milk", product)

        stale_date = datetime.now(tz=UTC) - timedelta(days=CACHE_MAX_AGE_DAYS + 1)
        conn = service._connect()
        try:
            conn.execute(
                "UPDATE product_mapping SET last_used = ?",
                (stale_date.isoformat(),),
            )
            conn.commit()
        finally:
            conn.close()

        assert service.get_cached_product("milk") is None

    def test_hit_touches_mapping(self, db_path: str) -> None:
        """Test a cache hit increments times_selected via _touch_mapping."""
        service = self._make_service(db_path)
        product = SafewayProduct(
            product_id="CACHE1", name="Cached Milk", price=4.99, size="1 gal"
        )
        service.save_mapping("milk", product)

        service.get_cached_product("milk")

        cached = service.get_cached_mapping("milk")
        assert cached is not None
        assert cached.times_selected == 2


# ------------------------------------------------------------------
# Tests: reverify_product
# ------------------------------------------------------------------


class TestReverifyProduct:
    """Tests for ProductSearchService.reverify_product (issue #71).

    A cache hit must not trust the stale cached price/size/stock: it
    re-runs a live search for the mapping's ingredient description and
    reconciles the cached product_id against the fresh candidates.
    """

    def _make_service_no_transport(self, db_path: str) -> ProductSearchService:
        """Create a service whose client would error on any HTTP call.

        Args:
            db_path: Database path.

        Returns:
            ProductSearchService instance.
        """
        transport = _MockTransport([])
        http = httpx.Client(transport=transport)
        client = SafewayClient("user", "pass", "1234", http_client=http)
        return ProductSearchService(client, db_path)

    def _cached(self, **overrides: Any) -> CachedMapping:
        """Build a CachedMapping for reverify_product scenarios.

        Args:
            overrides: SafewayProduct field overrides.

        Returns:
            A CachedMapping wrapping the (possibly overridden) product.
        """
        product_kwargs: dict[str, Any] = {
            "product_id": "P1",
            "name": "Chicken Thighs",
            "price": 3.99,
            "size": "1 lb",
            "in_stock": True,
        }
        product_kwargs.update(overrides)
        return CachedMapping(
            mapping_id=1,
            ingredient_description="chicken thighs",
            product=SafewayProduct(**product_kwargs),
            is_pinned=False,
            times_selected=1,
            last_used=datetime.now(tz=UTC),
        )

    @patch("grocery_butler.safeway_client.time.sleep")
    def test_matching_candidate_returns_fresh_values(
        self, mock_sleep: object, db_path: str
    ) -> None:
        """Test a matching product_id in fresh search results wins.

        Fresh price/size/stock must replace the cached values.
        """
        cached = self._cached()
        response_data = _make_search_response(
            [
                _make_nimbus_product(
                    upc="P1", name="Chicken Thighs", price=5.49, size="2 lb"
                ),
            ]
        )
        client, _transport = _make_authenticated_client(
            [httpx.Response(200, json=response_data)]
        )
        service = ProductSearchService(client, db_path)

        result = service.reverify_product(cached)

        assert result.product_id == "P1"
        assert result.price == Decimal("5.49")
        assert result.size == "2 lb"
        assert result.in_stock is True
        client.close()

    @patch("grocery_butler.safeway_client.time.sleep")
    def test_no_matching_candidate_marks_out_of_stock(
        self, mock_sleep: object, db_path: str
    ) -> None:
        """Test no product_id match marks the cached product out of stock.

        Regression guard for issue #71: this is the substitution-trigger
        path for cached items whose Safeway product has since vanished
        from search results (delisted or out of stock).
        """
        cached = self._cached()
        response_data = _make_search_response(
            [
                _make_nimbus_product(upc="OTHER1", name="Different Product"),
            ]
        )
        client, _transport = _make_authenticated_client(
            [httpx.Response(200, json=response_data)]
        )
        service = ProductSearchService(client, db_path)

        result = service.reverify_product(cached)

        assert result.product_id == "P1"
        assert result.price == Decimal("3.99")
        assert result.size == "1 lb"
        assert result.in_stock is False
        client.close()

    def test_search_error_returns_cached_product_unchanged(self, db_path: str) -> None:
        """Test a search failure falls back to the cached product untouched."""
        cached = self._cached()
        service = self._make_service_no_transport(db_path)

        with patch.object(
            service, "search_products", side_effect=ProductSearchError("down")
        ):
            result = service.reverify_product(cached)

        assert result == cached.product
        assert result.in_stock is True


# ------------------------------------------------------------------
# Tests: _is_stale
# ------------------------------------------------------------------


class TestIsStale:
    """Tests for ProductSearchService._is_stale."""

    def test_fresh_mapping_not_stale(self) -> None:
        """Test that a recently-used mapping is not stale."""
        cached = CachedMapping(
            mapping_id=1,
            ingredient_description="milk",
            product=SafewayProduct(
                product_id="1", name="Milk", price=3.99, size="1 gal"
            ),
            is_pinned=False,
            times_selected=1,
            last_used=datetime.now(tz=UTC),
        )
        assert ProductSearchService._is_stale(cached) is False

    def test_old_mapping_is_stale(self) -> None:
        """Test that an old mapping is considered stale."""
        old_date = datetime.now(tz=UTC) - timedelta(days=CACHE_MAX_AGE_DAYS + 1)
        cached = CachedMapping(
            mapping_id=1,
            ingredient_description="milk",
            product=SafewayProduct(
                product_id="1", name="Milk", price=3.99, size="1 gal"
            ),
            is_pinned=False,
            times_selected=1,
            last_used=old_date,
        )
        assert ProductSearchService._is_stale(cached) is True


# ------------------------------------------------------------------
# Tests: _row_to_cached_mapping
# ------------------------------------------------------------------


class TestRowToCachedMapping:
    """Tests for _row_to_cached_mapping."""

    def test_converts_row(self, db_path: str) -> None:
        """Test converting a database row to CachedMapping.

        Covers the real (post-migration-007) row shape: size and stock
        columns are populated and must be selected and rehydrated
        alongside the existing fields (issue #71).
        """
        from grocery_butler.db import get_connection, init_db

        init_db(db_path)
        conn = get_connection(db_path)
        try:
            conn.execute(
                "INSERT INTO product_mapping"
                " (ingredient_description, safeway_product_id,"
                "  safeway_product_name, safeway_price, safeway_product_size,"
                "  safeway_in_stock, is_pinned)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("milk", "UPC1", "Test Milk", 3.99, "1 gal", False, True),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id, ingredient_description, safeway_product_id,"
                " safeway_product_name, safeway_price, safeway_product_size,"
                " safeway_in_stock, last_used, times_selected, is_pinned"
                " FROM product_mapping LIMIT 1"
            ).fetchone()
            assert row is not None

            result = _row_to_cached_mapping(row)

            assert result.mapping_id == 1
            assert result.ingredient_description == "milk"
            assert result.product.product_id == "UPC1"
            assert result.product.name == "Test Milk"
            assert result.product.price == Decimal("3.99")
            assert result.product.size == "1 gal"
            assert result.product.in_stock is False
            assert result.is_pinned is True
            assert result.times_selected == 1
        finally:
            conn.close()
