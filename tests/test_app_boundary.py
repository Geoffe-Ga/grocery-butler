"""Integration tests for the tailnet-only network boundary (issue #62).

Exercises the real Flask app built by ``create_app`` end-to-end through
the test client to prove two things:

1. RED: today, state-mutating HTML routes and the dashboard respond to
   requests from a public (non-tailnet) source IP with normal 2xx/3xx
   behavior instead of being rejected. That is the vulnerability.
2. The full green-spec matrix the ``grocery_butler.network_guard`` module
   must satisfy once built: fail-closed defaults, the
   ``TAILNET_GUARD_ENABLED`` kill switch, CIDR overrides via
   ``TAILNET_GUARD_ALLOWED_CIDRS``, the ``/health``/``/healthz``
   exemption, X-Forwarded-For spoof resistance (admission is keyed only
   on ``request.remote_addr``), independence from the HMAC bearer check
   on ``/api/v1``, and the JSON-vs-HTML 403 response shape.

Because the module and its ``app.before_request`` hook do not exist yet,
every assertion of ``status_code == 403`` below currently fails against
today's ``grocery_butler/app.py`` -- that failure is the expected Gate 1
RED state for issue #62. Tests that assert *current* (pre-guard,
trusted-source) behavior are expected to pass now and continue passing
once the guard is implemented, since trusted sources are unaffected by
design.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient

from grocery_butler.app import create_app
from grocery_butler.auth_middleware import SECRET_ENV_VAR
from grocery_butler.models import IngredientCategory, InventoryItem, InventoryStatus
from grocery_butler.pantry_manager import PantryManager
from tests.conftest import bearer_header

#: A source address outside every default and custom-CIDR range used below.
PUBLIC_ADDR = "203.0.113.7"

#: Inside the default Tailscale CGNAT range (100.64.0.0/10).
CGNAT_ADDR = "100.64.1.2"

#: The Flask test client's own default REMOTE_ADDR (IPv4 loopback).
LOOPBACK_ADDR = "127.0.0.1"

#: Inside a custom override range used by the CIDR-override tests below.
CUSTOM_RANGE_ADDR = "192.0.2.10"

#: IPv4-mapped IPv6 form of a CGNAT address, as reported by a dual-stack
#: listener (bound to ``::`` with ``IPV6_V6ONLY`` off) for an IPv4 peer.
MAPPED_CGNAT_ADDR = "::ffff:100.64.1.2"

#: Shared HMAC secret for this module's bearer-token tests.
TEST_SECRET = "test-shared-secret-boundary"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a temporary database path for test isolation.

    Args:
        tmp_path: Pytest's per-test temporary directory.

    Returns:
        Absolute path string for a database file that does not yet exist.
    """
    return str(tmp_path / "test_boundary.db")


@pytest.fixture()
def make_app(
    monkeypatch: pytest.MonkeyPatch, db_path: str
) -> Callable[[dict[str, str] | None], Flask]:
    """Return a factory that builds a fresh app with given guard env vars.

    Always clears ``TAILNET_GUARD_ENABLED`` and
    ``TAILNET_GUARD_ALLOWED_CIDRS`` first (so each call starts from a
    clean, unconfigured slate), then applies any overrides, then calls
    ``create_app`` -- the guard's env config is read at registration
    time, so the env must be set before the factory runs, never after.

    Args:
        monkeypatch: Pytest's monkeypatch fixture.
        db_path: The temporary database path for this test.

    Returns:
        A callable ``(env=None) -> Flask`` that builds one fresh app per
        call, honoring the given guard environment variable overrides.
    """

    def _make(env: dict[str, str] | None = None) -> Flask:
        """Build one fresh Flask app with the given guard env overrides."""
        monkeypatch.delenv("TAILNET_GUARD_ENABLED", raising=False)
        monkeypatch.delenv("TAILNET_GUARD_ALLOWED_CIDRS", raising=False)
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
        application = create_app(db_path=db_path)
        application.config["TESTING"] = True
        return application

    return _make


@pytest.fixture()
def app(make_app: Callable[[dict[str, str] | None], Flask]) -> Flask:
    """Return the default app: no TAILNET_GUARD_* env set at all.

    This is the fail-closed-by-default configuration: the guard must be
    fully active with the production default CIDR list even though the
    test never set any TAILNET_GUARD_* variable.

    Args:
        make_app: Factory fixture for building apps with env overrides.

    Returns:
        A configured Flask application in TESTING mode.
    """
    return make_app(None)


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    """Return a Flask test client bound to the default app.

    Args:
        app: The default configured Flask application.

    Returns:
        A Flask test client.
    """
    return app.test_client()


@pytest.fixture()
def pantry_mgr(db_path: str) -> PantryManager:
    """Return a PantryManager bound to the test database.

    Args:
        db_path: The temporary database path for this test.

    Returns:
        A PantryManager instance.
    """
    return PantryManager(db_path)


@pytest.fixture()
def seeded_item(app: Flask, pantry_mgr: PantryManager) -> InventoryItem:
    """Seed a single trackable inventory item ("milk") for update tests.

    Depends on ``app`` (not just ``db_path``) so the schema exists
    before this fixture writes to it -- ``create_app`` is what runs
    ``init_db``.

    Args:
        app: The configured Flask application (forces schema creation
            before this fixture writes to the database).
        pantry_mgr: PantryManager bound to the same database.

    Returns:
        The InventoryItem as added to the database.
    """
    item = InventoryItem(
        ingredient="milk",
        display_name="Milk",
        category=IngredientCategory.DAIRY,
        status=InventoryStatus.ON_HAND,
    )
    pantry_mgr.add_item(item)
    return item


@pytest.fixture()
def auth_headers(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Return a valid Authorization header minted with a test shared secret.

    Bound to a bodyless ``GET /api/v1/inventory`` -- the only request
    this fixture is ever attached to in this module (issue #74: bearer
    tokens are now bound to their exact method/path/body).

    Args:
        monkeypatch: Pytest's monkeypatch fixture.

    Returns:
        A dict with a Bearer Authorization header signed with
        ``TEST_SECRET``.
    """
    monkeypatch.setenv(SECRET_ENV_VAR, TEST_SECRET)
    return bearer_header("rubotpaul", "GET", "/api/v1/inventory")


# ---------------------------------------------------------------------------
# RED headline: state-mutating routes and the dashboard reject public IPs
# ---------------------------------------------------------------------------


class TestStateMutatingRoutesRejectPublicSource:
    """Every state-mutating HTML route must 403 a public-source request."""

    def test_recipe_delete_public_source_forbidden(self, client: FlaskClient) -> None:
        """Test POST /recipes/<id>/delete from a public IP returns 403."""
        response = client.post(
            "/recipes/1/delete",
            environ_base={"REMOTE_ADDR": PUBLIC_ADDR},
        )
        assert response.status_code == 403

    def test_inventory_update_public_source_forbidden(
        self, client: FlaskClient, seeded_item: InventoryItem
    ) -> None:
        """Test POST /inventory/update from a public IP returns 403."""
        response = client.post(
            "/inventory/update",
            data=json.dumps({"ingredient": "milk", "status": "low"}),
            content_type="application/json",
            environ_base={"REMOTE_ADDR": PUBLIC_ADDR},
        )
        assert response.status_code == 403

    def test_brands_add_public_source_forbidden(self, client: FlaskClient) -> None:
        """Test POST /brands/add from a public IP returns 403."""
        response = client.post(
            "/brands/add",
            data={
                "match_target": "milk",
                "match_type": "ingredient",
                "brand": "Organic Valley",
                "preference_type": "preferred",
                "notes": "",
            },
            environ_base={"REMOTE_ADDR": PUBLIC_ADDR},
        )
        assert response.status_code == 403

    def test_preferences_save_public_source_forbidden(
        self, client: FlaskClient
    ) -> None:
        """Test POST /preferences from a public IP returns 403."""
        response = client.post(
            "/preferences",
            data={"default_servings": "6"},
            environ_base={"REMOTE_ADDR": PUBLIC_ADDR},
        )
        assert response.status_code == 403

    def test_dashboard_public_source_forbidden(self, client: FlaskClient) -> None:
        """Test GET / (dashboard) from a public IP returns 403."""
        response = client.get("/", environ_base={"REMOTE_ADDR": PUBLIC_ADDR})
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Trusted sources are unaffected
# ---------------------------------------------------------------------------


class TestAllowedSourcesNotAffected:
    """Loopback and CGNAT sources must see normal, pre-guard behavior."""

    @pytest.mark.parametrize("remote_addr", [LOOPBACK_ADDR, CGNAT_ADDR])
    def test_recipe_delete_trusted_source_not_forbidden(
        self, client: FlaskClient, remote_addr: str
    ) -> None:
        """Test recipe delete from loopback/CGNAT redirects normally."""
        response = client.post(
            "/recipes/1/delete",
            environ_base={"REMOTE_ADDR": remote_addr},
            follow_redirects=False,
        )
        assert response.status_code == 302

    @pytest.mark.parametrize("remote_addr", [LOOPBACK_ADDR, CGNAT_ADDR])
    def test_inventory_update_trusted_source_not_forbidden(
        self,
        client: FlaskClient,
        seeded_item: InventoryItem,
        remote_addr: str,
    ) -> None:
        """Test inventory update from loopback/CGNAT succeeds normally."""
        response = client.post(
            "/inventory/update",
            data=json.dumps({"ingredient": "milk", "status": "low"}),
            content_type="application/json",
            environ_base={"REMOTE_ADDR": remote_addr},
        )
        assert response.status_code == 200

    @pytest.mark.parametrize("remote_addr", [LOOPBACK_ADDR, CGNAT_ADDR])
    def test_brands_add_trusted_source_not_forbidden(
        self, client: FlaskClient, remote_addr: str
    ) -> None:
        """Test brand add from loopback/CGNAT redirects normally."""
        response = client.post(
            "/brands/add",
            data={
                "match_target": "milk",
                "match_type": "ingredient",
                "brand": "Organic Valley",
                "preference_type": "preferred",
                "notes": "",
            },
            environ_base={"REMOTE_ADDR": remote_addr},
        )
        assert response.status_code == 302

    @pytest.mark.parametrize("remote_addr", [LOOPBACK_ADDR, CGNAT_ADDR])
    def test_preferences_save_trusted_source_not_forbidden(
        self, client: FlaskClient, remote_addr: str
    ) -> None:
        """Test preferences save from loopback/CGNAT redirects normally."""
        response = client.post(
            "/preferences",
            data={"default_servings": "6"},
            environ_base={"REMOTE_ADDR": remote_addr},
        )
        assert response.status_code == 302

    @pytest.mark.parametrize("remote_addr", [LOOPBACK_ADDR, CGNAT_ADDR])
    def test_dashboard_trusted_source_not_forbidden(
        self, client: FlaskClient, remote_addr: str
    ) -> None:
        """Test dashboard GET from loopback/CGNAT renders normally."""
        response = client.get("/", environ_base={"REMOTE_ADDR": remote_addr})
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# X-Forwarded-For must never influence admission (spoof resistance)
# ---------------------------------------------------------------------------


class TestForwardedForSpoofResistance:
    """A public source cannot forge trust via X-Forwarded-For."""

    def test_public_source_xff_cgnat_still_forbidden(self, client: FlaskClient) -> None:
        """Test XFF claiming a CGNAT address does not bypass the guard."""
        response = client.get(
            "/",
            environ_base={"REMOTE_ADDR": PUBLIC_ADDR},
            headers={"X-Forwarded-For": CGNAT_ADDR},
        )
        assert response.status_code == 403

    def test_public_source_xff_loopback_still_forbidden(
        self, client: FlaskClient
    ) -> None:
        """Test XFF claiming loopback does not bypass the guard."""
        response = client.get(
            "/",
            environ_base={"REMOTE_ADDR": PUBLIC_ADDR},
            headers={"X-Forwarded-For": LOOPBACK_ADDR},
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Health exemption
# ---------------------------------------------------------------------------


class TestHealthExemption:
    """/health and /healthz must be reachable from any source."""

    def test_health_public_source_allowed(self, client: FlaskClient) -> None:
        """Test GET /health from a public IP still returns 200."""
        response = client.get("/health", environ_base={"REMOTE_ADDR": PUBLIC_ADDR})
        assert response.status_code == 200

    def test_healthz_public_source_allowed(self, client: FlaskClient) -> None:
        """Test GET /healthz from a public IP still returns 200."""
        response = client.get("/healthz", environ_base={"REMOTE_ADDR": PUBLIC_ADDR})
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Fail-closed default
# ---------------------------------------------------------------------------


class TestFailClosedDefault:
    """With no TAILNET_GUARD_* env set at all, public sources are rejected."""

    def test_no_guard_env_public_source_rejected(
        self, make_app: Callable[[dict[str, str] | None], Flask]
    ) -> None:
        """Test the guard is active by default with zero configuration."""
        application = make_app(None)
        client = application.test_client()

        response = client.get("/", environ_base={"REMOTE_ADDR": PUBLIC_ADDR})

        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


class TestKillSwitch:
    """TAILNET_GUARD_ENABLED can disable the guard entirely."""

    @pytest.mark.parametrize("value", ["false", "0", "no", "FALSE", "No"])
    def test_disabled_value_allows_public_source(
        self,
        make_app: Callable[[dict[str, str] | None], Flask],
        value: str,
    ) -> None:
        """Test each disabling spelling (case-insensitive) opens the guard."""
        application = make_app({"TAILNET_GUARD_ENABLED": value})
        client = application.test_client()

        response = client.get("/", environ_base={"REMOTE_ADDR": PUBLIC_ADDR})

        assert response.status_code == 200

    def test_enabled_value_still_rejects_public_source(
        self, make_app: Callable[[dict[str, str] | None], Flask]
    ) -> None:
        """Test an explicit enabling value keeps the guard active."""
        application = make_app({"TAILNET_GUARD_ENABLED": "true"})
        client = application.test_client()

        response = client.get("/", environ_base={"REMOTE_ADDR": PUBLIC_ADDR})

        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Custom CIDR override
# ---------------------------------------------------------------------------


class TestCustomCidrOverride:
    """TAILNET_GUARD_ALLOWED_CIDRS replaces (not extends) the default list."""

    @pytest.fixture()
    def custom_client(
        self, make_app: Callable[[dict[str, str] | None], Flask]
    ) -> FlaskClient:
        """Return a client for an app configured with a custom CIDR range.

        Args:
            make_app: Factory fixture for building apps with env overrides.

        Returns:
            A test client whose app only trusts 192.0.2.0/24.
        """
        application = make_app({"TAILNET_GUARD_ALLOWED_CIDRS": "192.0.2.0/24"})
        return application.test_client()

    def test_custom_range_source_allowed(self, custom_client: FlaskClient) -> None:
        """Test an address inside the custom range is trusted."""
        response = custom_client.get(
            "/", environ_base={"REMOTE_ADDR": CUSTOM_RANGE_ADDR}
        )
        assert response.status_code == 200

    def test_default_loopback_rejected_once_overridden(
        self, custom_client: FlaskClient
    ) -> None:
        """Test loopback is rejected once the default list is overridden.

        Proves the override *replaces* the default CIDR list rather than
        extending it.
        """
        response = custom_client.get("/", environ_base={"REMOTE_ADDR": LOOPBACK_ADDR})
        assert response.status_code == 403

    def test_public_source_still_rejected_under_override(
        self, custom_client: FlaskClient
    ) -> None:
        """Test an address outside the custom range is still rejected."""
        response = custom_client.get("/", environ_base={"REMOTE_ADDR": PUBLIC_ADDR})
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Explicitly-empty CIDR override: loud, fail-closed lockout
# ---------------------------------------------------------------------------


class TestExplicitlyEmptyCidrOverride:
    """TAILNET_GUARD_ALLOWED_CIDRS="" trusts nothing -- and says so loudly."""

    def test_empty_override_rejects_loopback(
        self, make_app: Callable[[dict[str, str] | None], Flask]
    ) -> None:
        """Test an explicitly-empty allow-list locks out even loopback."""
        application = make_app({"TAILNET_GUARD_ALLOWED_CIDRS": ""})
        client = application.test_client()

        response = client.get("/", environ_base={"REMOTE_ADDR": LOOPBACK_ADDR})

        assert response.status_code == 403

    def test_empty_override_health_still_exempt(
        self, make_app: Callable[[dict[str, str] | None], Flask]
    ) -> None:
        """Test /health stays reachable even under a trust-nothing list."""
        application = make_app({"TAILNET_GUARD_ALLOWED_CIDRS": ""})
        client = application.test_client()

        response = client.get("/health", environ_base={"REMOTE_ADDR": PUBLIC_ADDR})

        assert response.status_code == 200

    def test_empty_override_logs_startup_warning(
        self,
        make_app: Callable[[dict[str, str] | None], Flask],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test a blanked-out CIDR env var produces a loud startup warning.

        A stray empty-string value in a platform UI (e.g. Railway) must
        not cause a *silent* full lockout: the guard warns at startup so
        the misconfiguration is visible in the deploy logs.
        """
        with caplog.at_level(logging.WARNING, logger="network_guard"):
            make_app({"TAILNET_GUARD_ALLOWED_CIDRS": ""})

        warnings = [
            record
            for record in caplog.records
            if record.name == "network_guard" and record.levelno == logging.WARNING
        ]
        assert any("network_guard_empty_allowlist" in r.getMessage() for r in warnings)
        assert any("TAILNET_GUARD_ALLOWED_CIDRS" in r.getMessage() for r in warnings)


# ---------------------------------------------------------------------------
# Startup and rejection observability (supports live-deployment verification)
# ---------------------------------------------------------------------------


class TestGuardObservability:
    """The guard logs its resolved config at startup and every rejection."""

    def test_startup_logs_resolved_allowlist(
        self,
        make_app: Callable[[dict[str, str] | None], Flask],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test the resolved allow-list CIDRs are logged at startup.

        Operators verifying the guard against the live deployment need
        to see, in the deploy logs, exactly which CIDRs the running
        process resolved -- without reverse-engineering env precedence.
        """
        with caplog.at_level(logging.INFO, logger="network_guard"):
            make_app(None)

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "network_guard"
        ]
        enabled_lines = [m for m in messages if "network_guard_enabled" in m]
        assert enabled_lines
        assert any(
            "127.0.0.0/8" in m and "::1/128" in m and "100.64.0.0/10" in m
            for m in enabled_lines
        )

    def test_startup_logs_custom_override_cidrs(
        self,
        make_app: Callable[[dict[str, str] | None], Flask],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test a custom CIDR override is reflected in the startup log."""
        with caplog.at_level(logging.INFO, logger="network_guard"):
            make_app({"TAILNET_GUARD_ALLOWED_CIDRS": "192.0.2.0/24"})

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "network_guard"
        ]
        assert any(
            "network_guard_enabled" in m and "192.0.2.0/24" in m for m in messages
        )

    def test_rejection_logs_path_and_remote_addr(
        self,
        client: FlaskClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test each rejection logs the request path and observed peer.

        This is the log line operators use to confirm what
        ``request.remote_addr`` Railway's edge actually hands the app
        for public-domain traffic (see README "Verifying the boundary
        on the live deployment").
        """
        with caplog.at_level(logging.WARNING, logger="network_guard"):
            client.get("/", environ_base={"REMOTE_ADDR": PUBLIC_ADDR})

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "network_guard"
        ]
        assert any(
            "network_guard_rejected" in m and "path=/" in m and PUBLIC_ADDR in m
            for m in messages
        )


# ---------------------------------------------------------------------------
# IPv4-mapped IPv6 peers (dual-stack listener) are normalized, not rejected
# ---------------------------------------------------------------------------


class TestIpv4MappedPeers:
    """A dual-stack listener's ::ffff:a.b.c.d peers get IPv4 semantics."""

    def test_mapped_cgnat_source_allowed(self, client: FlaskClient) -> None:
        """Test an IPv4-mapped CGNAT peer reaches the dashboard normally."""
        response = client.get("/", environ_base={"REMOTE_ADDR": MAPPED_CGNAT_ADDR})
        assert response.status_code == 200

    def test_mapped_public_source_still_forbidden(self, client: FlaskClient) -> None:
        """Test an IPv4-mapped *public* peer is still rejected (403)."""
        response = client.get("/", environ_base={"REMOTE_ADDR": "::ffff:203.0.113.7"})
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


class TestResponseShape:
    """403 responses are JSON under /api/ and HTML everywhere else."""

    def test_api_path_public_source_json_body(self, client: FlaskClient) -> None:
        """Test a rejected /api/v1 request gets a JSON forbidden body."""
        response = client.get(
            "/api/v1/inventory", environ_base={"REMOTE_ADDR": PUBLIC_ADDR}
        )

        assert response.status_code == 403
        assert response.content_type == "application/json"
        assert response.get_json() == {"error": "forbidden"}

    def test_html_path_public_source_html_content_type(
        self, client: FlaskClient
    ) -> None:
        """Test a rejected HTML-page request gets an HTML error page."""
        response = client.get("/", environ_base={"REMOTE_ADDR": PUBLIC_ADDR})

        assert response.status_code == 403
        assert "text/html" in response.content_type


# ---------------------------------------------------------------------------
# The IP guard and the HMAC bearer are independent gates on /api/v1
# ---------------------------------------------------------------------------


class TestApiGuardIndependentOfBearer:
    """A valid bearer token does not bypass the IP guard, and vice versa."""

    def test_valid_bearer_public_source_still_forbidden(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """Test a valid bearer from a public IP is still rejected by the
        guard (403, not 200) -- the two gates are independent.
        """
        response = client.get(
            "/api/v1/inventory",
            headers=auth_headers,
            environ_base={"REMOTE_ADDR": PUBLIC_ADDR},
        )
        assert response.status_code == 403

    def test_valid_bearer_loopback_source_succeeds(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """Test a valid bearer from loopback passes both gates (200)."""
        response = client.get(
            "/api/v1/inventory",
            headers=auth_headers,
            environ_base={"REMOTE_ADDR": LOOPBACK_ADDR},
        )
        assert response.status_code == 200
