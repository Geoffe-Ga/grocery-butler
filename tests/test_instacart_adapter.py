"""Tests for the Instacart adapter skeleton (ADR 0014, #146).

Nothing here reaches a network: the adapter is driven by a fake transport.
"""

from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from grocery_butler.fulfillment.base import (
    Authorization,
    Capability,
    HandoffStatus,
    UnsupportedCapabilityError,
    unenforced_constraints,
)
from grocery_butler.fulfillment.instacart.adapter import (
    ADAPTER_NAME,
    DEFAULT_LINK_DAYS,
    InstacartAdapter,
)
from grocery_butler.fulfillment.instacart.models import (
    Filters,
    LineItem,
    Measurement,
    ProductsLinkRequest,
    ProductsLinkResponse,
)
from grocery_butler.fulfillment.instacart.transport import (
    STUB_LINK_PREFIX,
    FakeTransport,
    StubTransport,
    TransportError,
)
from grocery_butler.fulfillment.registry import (
    INSTACART_ENABLED_ENV,
    default_registry,
    instacart_enabled,
)
from grocery_butler.fulfillment.runner import create_handoff
from grocery_butler.models import (
    HardConstraintKind,
    IngredientCategory,
    NeedReasonKind,
    PlanAmount,
    PlanHardConstraint,
    PlanLine,
    PlanReason,
    ShoppingPlan,
    Unit,
)
from grocery_butler.retries import RetriesExhaustedError

NOW = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.UTC)
PACKAGE = Path(__file__).resolve().parent.parent / "grocery_butler"
INSTACART = "grocery_butler.fulfillment.instacart"
LINK = "https://example.invalid/l/abc"


def _clock() -> dt.datetime:
    """Return a fixed time."""
    return NOW


def _line(item_id: int, name: str, must: list[PlanHardConstraint]) -> PlanLine:
    """Build one plan line."""
    return PlanLine(
        item_id=item_id,
        display_name=name,
        category=IngredientCategory.PRODUCE,
        amounts=[PlanAmount(quantity=2.0, unit=Unit.LB)],
        reasons=[
            PlanReason(kind=NeedReasonKind.MANUAL_REQUEST, source_key="test", detail={})
        ],
        constraints={"preferred_brand": "Clover"},
        need_id=item_id,
        hard_constraints=must,
    )


def _plan(version: int = 7, *, lines: bool = True) -> ShoppingPlan:
    """Build a plan with two lines, one carrying an allergen exclusion."""
    peanut = PlanHardConstraint(
        kind=HardConstraintKind.ALLERGEN_EXCLUSION, value="peanut", scope="household"
    )
    return ShoppingPlan(
        id=version,
        version=version,
        content_hash=f"hash-{version}",
        meals=[],
        unplanned=[],
        created_by="geoff@test",
        created_at=NOW,
        lines=(
            [_line(1, "Carrots", []), _line(2, "Granola Bars", [peanut])]
            if lines
            else []
        ),
    )


def _imports(path: Path) -> set[str]:
    """Return every module a source file imports."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


class TestDescription:
    """The adapter claims exactly what ADR 0014 allows."""

    def test_it_claims_only_the_shopping_list_link(self) -> None:
        """One capability, and none that reads anything back."""
        description = InstacartAdapter(FakeTransport.ok(LINK)).describe()

        assert description.name == ADAPTER_NAME == "instacart"
        assert description.capabilities == frozenset({Capability.SHOPPING_LIST_LINK})
        assert description.authorization is Authorization.PROVIDER_PROGRAM
        assert description.human_required is True
        assert description.enforces == frozenset()
        assert "Nothing is read back" in description.data_freshness

    @pytest.mark.parametrize(
        "capability",
        [
            Capability.READ_CART,
            Capability.READ_ORDER,
            Capability.IMPORT_RECEIPT,
            Capability.DRAFT_CART,
            Capability.DEEP_LINK_CART,
        ],
    )
    def test_it_refuses_what_it_did_not_declare(self, capability: Capability) -> None:
        """A caller relying on a cart or an order is stopped."""
        adapter = InstacartAdapter(FakeTransport.ok(LINK))

        with pytest.raises(UnsupportedCapabilityError, match=capability.value):
            adapter.require(capability)

    @pytest.mark.parametrize("days", [0, -1, 366])
    def test_link_days_must_be_in_range(self, days: int) -> None:
        """A link lives 1 to 365 days."""
        with pytest.raises(ValueError, match="between 1 and 365"):
            InstacartAdapter(FakeTransport.ok(LINK), link_days=days)


class TestHandoff:
    """A plan becomes a link, or a truthful failure."""

    def test_created_handoff(self) -> None:
        """One request, one link, with an expiry and no claim of purchase."""
        transport = FakeTransport.ok(LINK)
        adapter = InstacartAdapter(transport, clock=_clock)
        plan = _plan()

        result = adapter.create_handoff(plan)

        assert result.status is HandoffStatus.CREATED
        assert result.adapter == "instacart"
        assert result.external_url == LINK
        assert result.plan_version == 7
        assert result.plan_content_hash == "hash-7"
        assert result.expires_at == NOW + dt.timedelta(days=DEFAULT_LINK_DAYS)
        assert "check out there yourself" in result.human_action
        assert result.detail == ""
        assert len(transport.requests) == 1

    def test_the_request_names_each_line_and_always_sets_an_expiry(self) -> None:
        """The skeleton sends names only, and never omits expires_in."""
        transport = FakeTransport.ok(LINK)

        InstacartAdapter(transport, link_days=5).create_handoff(_plan())

        assert transport.requests[0].body() == {
            "title": "Grocery Butler shopping plan 7",
            "link_type": "shopping_list",
            "expires_in": 5,
            "line_items": [
                {"name": "Carrots", "line_item_measurements": []},
                {"name": "Granola Bars", "line_item_measurements": []},
            ],
        }

    def test_hard_constraints_are_reported_never_sent_as_filters(self) -> None:
        """An allergen exclusion is unenforced, and is not turned into a filter."""
        transport = FakeTransport.ok(LINK)
        adapter = InstacartAdapter(transport)
        plan = _plan()

        result = adapter.create_handoff(plan)

        assert [(c.display_name, c.value) for c in result.unenforced_constraints] == [
            ("Granola Bars", "peanut")
        ]
        assert result.unenforced_constraints == unenforced_constraints(
            adapter.describe(), plan
        )
        assert all(item.filters is None for item in transport.requests[0].line_items)
        assert "peanut" not in str(transport.requests[0].body())

    def test_an_empty_plan_is_a_failed_handoff_without_a_request(self) -> None:
        """Nothing to send means nothing is sent."""
        transport = FakeTransport.ok(LINK)

        result = InstacartAdapter(transport).create_handoff(_plan(lines=False))

        assert result.status is HandoffStatus.FAILED
        assert result.external_url is None
        assert "no lines" in result.detail
        assert transport.requests == []

    @pytest.mark.parametrize(
        ("code", "status"),
        [("bad_request", 400), ("unauthorized", 401), ("not_configured", None)],
    )
    def test_a_permanent_provider_error_is_a_failed_handoff(
        self, code: str, status: int | None
    ) -> None:
        """A refusal is reported with its stable code, and never as created."""
        adapter = InstacartAdapter(FakeTransport.failing(code, status))

        result = adapter.create_handoff(_plan())

        assert result.status is HandoffStatus.FAILED
        assert result.detail == code
        assert result.external_url is None
        assert result.expires_at is None
        assert "No Instacart link was created" in result.human_action
        assert len(result.unenforced_constraints) == 1

    @pytest.mark.parametrize(
        ("code", "status"), [("rate_limited", 429), ("unavailable", 503)]
    )
    def test_a_passing_provider_fault_is_raised_for_the_runner(
        self, code: str, status: int
    ) -> None:
        """A rate limit or outage is the runner's to retry."""
        adapter = InstacartAdapter(FakeTransport.failing(code, status))

        with pytest.raises(TransportError, match=code) as raised:
            adapter.create_handoff(_plan())

        assert raised.value.status_code == status

    @pytest.mark.parametrize("link", ["", "http://insecure.example/l", "https://", "x"])
    def test_a_response_without_a_usable_link_is_failed_never_created(
        self, link: str
    ) -> None:
        """A 200 with no HTTPS link is not a handoff."""
        result = InstacartAdapter(FakeTransport.ok(link)).create_handoff(_plan())

        assert result.status is HandoffStatus.FAILED
        assert result.detail == "invalid_response"
        assert result.external_url is None


class TestThroughTheRunner:
    """The shared runner retries and audits this adapter like any other."""

    def test_a_rate_limit_then_success(self) -> None:
        """One 429, then the link: two requests, one handoff."""
        transport = FakeTransport(TransportError("rate_limited", 429), LINK)
        slept: list[float] = []

        result = create_handoff(
            InstacartAdapter(transport), _plan(), sleep=slept.append
        )

        assert result.status is HandoffStatus.CREATED
        assert len(transport.requests) == 2
        assert slept == [0.5]

    def test_an_outage_gives_up_after_three_requests(self) -> None:
        """The retry budget bounds what is sent to the provider."""
        transport = FakeTransport.failing("unavailable", 503)

        with pytest.raises(RetriesExhaustedError):
            create_handoff(InstacartAdapter(transport), _plan(), sleep=lambda _s: None)

        assert len(transport.requests) == 3

    def test_a_refusal_is_not_retried(self) -> None:
        """A 400 is sent once."""
        transport = FakeTransport.failing("bad_request", 400)

        result = create_handoff(InstacartAdapter(transport), _plan())

        assert result.status is HandoffStatus.FAILED
        assert len(transport.requests) == 1


class TestStubTransport:
    """The demo transport: a fake link, the same every time."""

    def test_the_link_is_deterministic_per_plan_version(self) -> None:
        """The same plan version always gives the same link."""
        first = InstacartAdapter(StubTransport()).create_handoff(_plan(7))
        again = InstacartAdapter(StubTransport()).create_handoff(_plan(7))
        other = InstacartAdapter(StubTransport()).create_handoff(_plan(8))

        assert first.status is HandoffStatus.CREATED
        assert first.external_url == again.external_url
        assert first.external_url != other.external_url

    def test_the_link_is_obviously_fake_and_can_not_resolve(self) -> None:
        """It points at the reserved .invalid domain."""
        result = InstacartAdapter(StubTransport()).create_handoff(_plan())

        assert result.external_url is not None
        assert result.external_url.startswith(STUB_LINK_PREFIX)
        assert "example.invalid/instacart-stub/" in result.external_url


class TestModels:
    """The typed boundary refuses what the documentation doesn't describe."""

    def test_measurements_and_filters_render_as_documented(self) -> None:
        """A full line item has the documented field names."""
        request = ProductsLinkRequest(
            title="List",
            expires_in=7,
            line_items=[
                LineItem(
                    name="carrots",
                    display_text="Carrots",
                    line_item_measurements=[Measurement(quantity=2, unit="pound")],
                    filters=Filters(health_filters=["ORGANIC"]),
                )
            ],
        )

        assert request.body()["line_items"] == [
            {
                "name": "carrots",
                "display_text": "Carrots",
                "line_item_measurements": [{"quantity": 2.0, "unit": "pound"}],
                "filters": {"brand_filters": [], "health_filters": ["ORGANIC"]},
            }
        ]

    @pytest.mark.parametrize(
        "fields",
        [
            {"title": "", "expires_in": 7, "line_items": [{"name": "x"}]},
            {"title": "List", "expires_in": 0, "line_items": [{"name": "x"}]},
            {"title": "List", "expires_in": 366, "line_items": [{"name": "x"}]},
            {"title": "List", "expires_in": 7, "line_items": []},
            {"title": "List", "expires_in": 7, "line_items": [{"name": ""}]},
            {"title": "List", "expires_in": 7, "line_items": [{"name": "x"}], "x": 1},
        ],
    )
    def test_invalid_requests_are_refused(self, fields: dict[str, Any]) -> None:
        """A request the provider would reject is never built."""
        with pytest.raises(ValidationError):
            ProductsLinkRequest(**fields)

    @pytest.mark.parametrize("quantity", [0, -1])
    def test_a_measurement_needs_a_positive_quantity(self, quantity: float) -> None:
        """Zero or less is a documented 400, so it is refused here."""
        with pytest.raises(ValidationError):
            Measurement(quantity=quantity, unit="each")

    def test_models_are_immutable(self) -> None:
        """A request can't be altered after it is built."""
        response = ProductsLinkResponse(products_link_url=LINK)

        with pytest.raises(ValidationError):
            response.products_link_url = "https://elsewhere.example"

    def test_transport_error_message_carries_only_the_code(self) -> None:
        """An error says a stable code, nothing from the provider."""
        error = TransportError("rate_limited", 429)

        assert str(error) == "The provider request failed: rate_limited."
        assert (error.code, error.status_code) == ("rate_limited", 429)


class TestRegistry:
    """Instacart is off unless the deployment turns it on."""

    def test_disabled_by_default(self) -> None:
        """With nothing set, only the list export is offered."""
        assert default_registry({}).names() == ["export"]

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes "])
    def test_enabled_by_the_environment(self, value: str) -> None:
        """A truthy INSTACART_ENABLED adds the adapter, on the stub transport."""
        registry = default_registry({INSTACART_ENABLED_ENV: value})

        assert registry.names() == ["export", "instacart"]
        result = registry.get("instacart").create_handoff(_plan())
        assert result.external_url is not None
        assert result.external_url.startswith(STUB_LINK_PREFIX)

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "enabled"])
    def test_anything_else_leaves_it_off(self, value: str) -> None:
        """Only an explicit yes turns it on."""
        assert instacart_enabled({INSTACART_ENABLED_ENV: value}) is False
        assert default_registry({INSTACART_ENABLED_ENV: value}).names() == ["export"]

    def test_the_process_environment_is_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no mapping given, the real environment decides."""
        monkeypatch.delenv(INSTACART_ENABLED_ENV, raising=False)
        assert default_registry().names() == ["export"]

        monkeypatch.setenv(INSTACART_ENABLED_ENV, "true")
        assert default_registry().names() == ["export", "instacart"]


class TestBoundary:
    """Instacart types stay inside the Instacart package (ADR 0001)."""

    def _sources(self) -> list[Path]:
        """Every package source file outside the Instacart package."""
        return [
            path
            for path in PACKAGE.rglob("*.py")
            if "fulfillment/instacart" not in path.as_posix()
        ]

    def test_only_the_registry_imports_the_instacart_package(self) -> None:
        """No core module, API or CLI knows the Instacart package exists."""
        importers = sorted(
            path.relative_to(PACKAGE).as_posix()
            for path in self._sources()
            if any(name.startswith(INSTACART) for name in _imports(path))
        )

        assert importers == ["fulfillment/registry.py"]

    def test_the_boundary_check_sees_real_files(self) -> None:
        """The scan covers the package, so an empty scan can't pass by accident."""
        names = {path.name for path in self._sources()}

        assert {"models.py", "api_v2.py", "shopping_plans.py", "registry.py"} <= names

    def test_the_package_can_not_reach_a_network(self) -> None:
        """No HTTP or socket library is imported here until #148."""
        network = ("requests", "httpx", "httpx2", "urllib", "http", "socket", "aiohttp")
        offending = sorted(
            f"{path.name}: {name}"
            for path in (PACKAGE / "fulfillment" / "instacart").glob("*.py")
            for name in _imports(path)
            if name.split(".")[0] in network
        )

        assert offending == []
