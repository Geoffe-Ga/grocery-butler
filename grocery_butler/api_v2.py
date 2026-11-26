"""Capability API for OpenClaw at ``/api/v2`` (ADR 0009).

The contract is ``docs/api/v2.md``. This module is a channel adapter: it
authenticates, resolves the acting household member, enforces idempotency,
calls one domain service per capability, and shapes JSON. No business rule
lives here.

Every request carries the same HMAC bearer token as ``/api/v1``. Every
mutation also carries ``X-Household-Member`` and ``Idempotency-Key``. Every
error body is ``{"error": {"code", "message", "details"}}``.
"""

from __future__ import annotations

import functools
import json
import logging
from typing import TYPE_CHECKING, Any

from flask import Blueprint, Response, current_app, g, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from werkzeug.exceptions import HTTPException

from grocery_butler.auth_middleware import require_bearer
from grocery_butler.household_items import (
    AliasConflictError,
    HouseholdItemStore,
    ItemNotFoundError,
    MergeError,
)
from grocery_butler.idempotency import (
    ClaimOutcome,
    IdempotencyStore,
    is_valid_key,
    request_hash,
)
from grocery_butler.inventory_ledger import InventoryLedger
from grocery_butler.members import (
    InvalidMemberKeyError,
    MemberExistsError,
    MemberNotFoundError,
    MemberStore,
)
from grocery_butler.models import HouseholdItemUpdate, IngredientCategory
from grocery_butler.replenishment import ReplenishmentNeeds

if TYPE_CHECKING:
    from collections.abc import Callable

    from grocery_butler.models import HouseholdItem

LOG = logging.getLogger("api_v2")

API_V2_PREFIX = "/api/v2"
api_v2 = Blueprint("api_v2", __name__, url_prefix=API_V2_PREFIX)

MEMBER_HEADER = "X-Household-Member"
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotency-Replayed"

JsonResult = tuple[dict[str, Any], int]

# Stable error code for each HTTP status the framework itself raises.
_STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    413: "payload_too_large",
    500: "internal_error",
}

# Stable error code and status for each domain error a capability can raise.
_DOMAIN_ERRORS: dict[type[Exception], tuple[int, str]] = {
    ItemNotFoundError: (404, "item_not_found"),
    AliasConflictError: (409, "alias_conflict"),
    MergeError: (409, "merge_not_allowed"),
    MemberExistsError: (409, "member_exists"),
    MemberNotFoundError: (404, "member_not_found"),
    InvalidMemberKeyError: (400, "invalid_member_key"),
}


class ApiError(Exception):
    """An error with a stable code, raised by the blueprint's own checks."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Describe the error.

        Args:
            status: HTTP status to respond with.
            code: Stable, documented error code.
            message: What went wrong and what to do, for a person.
            details: Machine-readable extras, if any.
        """
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def error_body(
    code: str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build the v2 error envelope.

    Args:
        code: Stable, documented error code.
        message: What went wrong, for a person.
        details: Machine-readable extras, if any.

    Returns:
        ``{"error": {"code", "message", "details"}}``.
    """
    return {"error": {"code": code, "message": message, "details": details or {}}}


def framework_error(status: int, message: str) -> Response:
    """Build the envelope for an error the framework raised.

    Used by the app-level error handlers for ``/api/v2`` paths (an unknown
    route, a wrong method, an oversized body, an uncaught exception), which
    Flask resolves before this blueprint's own handlers.

    Args:
        status: The HTTP status.
        message: A short description of the failure.

    Returns:
        The JSON error response. The caller supplies the status code.
    """
    code = _STATUS_CODES.get(status, "http_error")
    return _json(error_body(code, message), status)


def _json(payload: dict[str, Any], status: int) -> Response:
    """Serialize a payload as a JSON response with stable key order.

    Args:
        payload: The response body.
        status: The HTTP status.

    Returns:
        The response.
    """
    return Response(
        json.dumps(payload, sort_keys=True), status=status, mimetype="application/json"
    )


# ---------------------------------------------------------------------------
# Authentication and error handling
# ---------------------------------------------------------------------------


@api_v2.before_request
def _enforce_bearer_auth() -> None:
    """Require a valid bearer token for every request on this blueprint.

    Raises:
        werkzeug.exceptions.HTTPException: 401 when the token is missing or
            invalid for this exact request.
    """
    g.caller_id = require_bearer()


@api_v2.errorhandler(ApiError)
def _handle_api_error(exc: ApiError) -> Response:
    """Render one of the blueprint's own errors.

    Args:
        exc: The error.

    Returns:
        The error envelope with the error's status.
    """
    return _json(error_body(exc.code, exc.message, exc.details), exc.status)


@api_v2.errorhandler(ValidationError)
def _handle_validation_error(exc: ValidationError) -> Response:
    """Render a request body that failed validation.

    Args:
        exc: The pydantic validation error.

    Returns:
        A 400 naming each rejected field, without echoing its value.
    """
    fields = [
        {"field": ".".join(str(part) for part in err["loc"]), "problem": err["msg"]}
        for err in exc.errors()
    ]
    return _json(
        error_body(
            "validation_failed",
            "The request body is not valid. See details.fields.",
            {"fields": fields},
        ),
        400,
    )


@api_v2.errorhandler(HTTPException)
def _handle_http_error(exc: HTTPException) -> Response:
    """Render a framework error (bad JSON, missing token) as an envelope.

    Args:
        exc: The HTTP exception.

    Returns:
        The error envelope with the exception's status.
    """
    status = exc.code or 500
    code = _STATUS_CODES.get(status, "http_error")
    return _json(error_body(code, exc.description or code), status)


def _handle_domain_error(exc: Exception) -> Response:
    """Render a domain error with its documented code.

    Args:
        exc: An instance of one of the ``_DOMAIN_ERRORS`` types.

    Returns:
        The error envelope with the mapped status.
    """
    status, code = _DOMAIN_ERRORS[type(exc)]
    return _json(error_body(code, str(exc)), status)


for _error_type in _DOMAIN_ERRORS:
    api_v2.register_error_handler(_error_type, _handle_domain_error)


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


def _db_path() -> str:
    """Return the app's database path.

    Returns:
        The configured SQLite path or PostgreSQL URL.
    """
    path: str = current_app.config["DATABASE_PATH"]
    return path


def _body() -> dict[str, Any]:
    """Return the request's JSON object body.

    Returns:
        The parsed object.

    Raises:
        ApiError: 400 ``bad_request`` when the body is not a JSON object.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request", "The request body must be a JSON object.")
    return body


def _resolve_actor(*, allow_bootstrap: bool) -> str:
    """Work out who a mutation is attributed to.

    Args:
        allow_bootstrap: Let the header be omitted while the household has
            no active member yet (creating the first one).

    Returns:
        ``member@channel``, or the channel alone during bootstrap.

    Raises:
        ApiError: 400 ``member_required`` when the header is missing, or
            403 ``member_unknown`` when it names no active member.
    """
    channel: str = g.caller_id
    members = MemberStore(_db_path())
    raw = request.headers.get(MEMBER_HEADER, "").strip()
    if not raw:
        if allow_bootstrap and not members.list_members():
            return channel
        raise ApiError(
            400,
            "member_required",
            f"Send the acting household member's key in the {MEMBER_HEADER} header.",
        )
    member = members.get(raw)
    if member is None or not member.active:
        raise ApiError(
            403,
            "member_unknown",
            f'"{raw}" is not an active household member.',
        )
    return f"{member.key}@{channel}"


def _claim_key(actor: str) -> tuple[IdempotencyStore, str, Response | None]:
    """Claim the request's idempotency key.

    Args:
        actor: The resolved actor, part of the request's fingerprint.

    Returns:
        The store, the key, and a stored response to replay (or None when
        the request should run).

    Raises:
        ApiError: 400 when the key is missing or malformed, 409 when the
            same request is still running or the key was used for a
            different request.
    """
    key = request.headers.get(IDEMPOTENCY_HEADER, "")
    if not key:
        raise ApiError(
            400,
            "idempotency_key_required",
            f"Send a unique {IDEMPOTENCY_HEADER} header with every change.",
        )
    if not is_valid_key(key):
        raise ApiError(
            400,
            "idempotency_key_invalid",
            f"{IDEMPOTENCY_HEADER} must be 8 to 128 printable ASCII characters "
            "with no spaces.",
        )
    store = IdempotencyStore(_db_path())
    fingerprint = request_hash(
        request.method, request.path, request.get_data() + b"\n" + actor.encode()
    )
    claim = store.claim(key, fingerprint)
    if claim.outcome is ClaimOutcome.MISMATCH:
        raise ApiError(
            409,
            "idempotency_key_reused",
            "This Idempotency-Key was already used for a different request. "
            "Use a new key for a new change.",
        )
    if claim.outcome is ClaimOutcome.IN_PROGRESS:
        raise ApiError(
            409,
            "request_in_progress",
            "A request with this Idempotency-Key is still running. Retry shortly.",
        )
    if claim.outcome is ClaimOutcome.REPLAY:
        replay = Response(
            claim.response_body,
            status=claim.status_code or 200,
            mimetype="application/json",
        )
        replay.headers[REPLAY_HEADER] = "true"
        return store, key, replay
    return store, key, None


def mutation(
    *, allow_bootstrap: bool = False
) -> Callable[[Callable[..., JsonResult]], Callable[..., Response]]:
    """Wrap a view that changes state with attribution and idempotency.

    The wrapped view reads the actor from ``g.actor`` and returns
    ``(payload, status)``. A successful response is stored under the
    request's idempotency key; a failure releases the key.

    Args:
        allow_bootstrap: Let the member header be omitted while the
            household has no active member.

    Returns:
        A decorator for capability views.
    """

    def decorate(view: Callable[..., JsonResult]) -> Callable[..., Response]:
        """Wrap one view.

        Args:
            view: The capability view.

        Returns:
            The wrapped view.
        """

        @functools.wraps(view)
        def wrapper(**kwargs: Any) -> Response:
            """Resolve the actor, claim the key, run the view, store the result.

            Args:
                **kwargs: URL parameters for the view.

            Returns:
                The view's response, or a stored one when replaying.
            """
            g.actor = _resolve_actor(allow_bootstrap=allow_bootstrap)
            store, key, replay = _claim_key(g.actor)
            if replay is not None:
                return replay
            try:
                payload, status = view(**kwargs)
            except Exception:
                store.release(key)
                raise
            response = _json(payload, status)
            store.complete(key, status, response.get_data(as_text=True))
            LOG.info(
                "mutation endpoint=%s actor=%s status=%s",
                request.endpoint,
                g.actor,
                status,
            )
            return response

        return wrapper

    return decorate


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


class _NewMember(BaseModel):
    """Body of ``POST /members``."""

    model_config = ConfigDict(extra="forbid")

    key: str
    display_name: str = Field(min_length=1)


@api_v2.get("/members")
def list_members() -> Response:
    """List household members.

    Returns:
        ``{"members": [...]}``, active only unless ``include_inactive=true``.
    """
    include = request.args.get("include_inactive") == "true"
    members = MemberStore(_db_path()).list_members(include_inactive=include)
    return _json({"members": [m.model_dump(mode="json") for m in members]}, 200)


@api_v2.post("/members")
@mutation(allow_bootstrap=True)
def add_member() -> JsonResult:
    """Add a household member.

    Returns:
        The new member, with 201.
    """
    body = _NewMember.model_validate(_body())
    member = MemberStore(_db_path()).add(
        body.key, body.display_name, created_by=g.actor
    )
    return member.model_dump(mode="json"), 201


@api_v2.post("/members/<key>/deactivate")
@mutation()
def deactivate_member(key: str) -> JsonResult:
    """Stop a member being named as the actor of new changes.

    Args:
        key: The member's key.

    Returns:
        The member after the change.
    """
    member = MemberStore(_db_path()).set_active(key, active=False)
    return member.model_dump(mode="json"), 200


@api_v2.post("/members/<key>/reactivate")
@mutation()
def reactivate_member(key: str) -> JsonResult:
    """Let a deactivated member act again.

    Args:
        key: The member's key.

    Returns:
        The member after the change.
    """
    member = MemberStore(_db_path()).set_active(key, active=True)
    return member.model_dump(mode="json"), 200


# ---------------------------------------------------------------------------
# Household items and their current state
# ---------------------------------------------------------------------------


class _NewItem(BaseModel):
    """Body of ``POST /items``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    display_name: str | None = None
    category: IngredientCategory | None = None


class _ItemChanges(HouseholdItemUpdate):
    """Body of ``PATCH /items/<id>``."""

    model_config = ConfigDict(extra="forbid")


class _Alias(BaseModel):
    """Body of the alias capabilities."""

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)


class _MergeTarget(BaseModel):
    """Body of ``POST /items/<id>/merge``."""

    model_config = ConfigDict(extra="forbid")

    into_item_id: int


def _item_payload(item: HouseholdItem, *, detailed: bool) -> dict[str, Any]:
    """Shape an item, with what we currently believe about it.

    Args:
        item: The item.
        detailed: Also include aliases and the open replenishment need.

    Returns:
        The item's JSON object. ``state`` is the snapshot of its canonical
        item (ADR 0007), so a merged item reports its survivor's state.
    """
    db_path = _db_path()
    payload: dict[str, Any] = item.model_dump(mode="json")
    payload["state"] = (
        InventoryLedger(db_path).snapshot(item.id).model_dump(mode="json")
    )
    if detailed:
        need = ReplenishmentNeeds(db_path).open_need_for_item(item.id)
        payload["aliases"] = HouseholdItemStore(db_path).aliases(item.id)
        payload["open_need"] = need.model_dump(mode="json") if need else None
    return payload


def _require_item(item_id: int) -> HouseholdItem:
    """Fetch an item by id.

    Args:
        item_id: The item's id.

    Returns:
        The item, merged or not.

    Raises:
        ItemNotFoundError: If no item has this id.
    """
    item = HouseholdItemStore(_db_path()).get(item_id)
    if item is None:
        raise ItemNotFoundError(item_id)
    return item


@api_v2.get("/items")
def list_items() -> Response:
    """List household items with their current state.

    Query: ``q`` filters by a case-insensitive match on name, display name
    or alias; ``include_inactive=true`` also returns merged items.

    Returns:
        ``{"items": [...]}``, sorted by name.
    """
    store = HouseholdItemStore(_db_path())
    include = request.args.get("include_inactive") == "true"
    query = request.args.get("q", "").strip().lower()
    items = store.list_items(include_inactive=include)
    if query:
        items = [
            item
            for item in items
            if query in item.name
            or query in item.display_name.lower()
            or any(query in alias for alias in store.aliases(item.id))
        ]
    return _json(
        {"items": [_item_payload(item, detailed=False) for item in items]}, 200
    )


@api_v2.post("/items")
@mutation()
def create_item() -> JsonResult:
    """Find the item a name refers to, or create it.

    Returns:
        The canonical item: 201 when it was created, 200 when it existed.
    """
    body = _NewItem.model_validate(_body())
    store = HouseholdItemStore(_db_path())
    existing = store.resolve(body.name)
    item = existing or store.resolve_or_create(
        body.name,
        actor=g.actor,
        display_name=body.display_name,
        category=body.category,
    )
    return _item_payload(item, detailed=True), 200 if existing else 201


@api_v2.get("/items/<int:item_id>")
def get_item(item_id: int) -> Response:
    """Return one item with its aliases, state and open need.

    Args:
        item_id: The item's id.

    Returns:
        The item's JSON object.
    """
    return _json(_item_payload(_require_item(item_id), detailed=True), 200)


@api_v2.patch("/items/<int:item_id>")
@mutation()
def update_item(item_id: int) -> JsonResult:
    """Change an item's display name, category, policy or preferences.

    Args:
        item_id: The item's id.

    Returns:
        The item after the change.
    """
    changes = _ItemChanges.model_validate(_body())
    item = HouseholdItemStore(_db_path()).update(item_id, changes, actor=g.actor)
    return _item_payload(item, detailed=True), 200


@api_v2.post("/items/<int:item_id>/aliases")
@mutation()
def add_alias(item_id: int) -> JsonResult:
    """Give an item another name.

    Args:
        item_id: The item's id.

    Returns:
        The item after the change.
    """
    body = _Alias.model_validate(_body())
    HouseholdItemStore(_db_path()).add_alias(item_id, body.alias, actor=g.actor)
    return _item_payload(_require_item(item_id), detailed=True), 200


@api_v2.post("/items/<int:item_id>/aliases/remove")
@mutation()
def remove_alias(item_id: int) -> JsonResult:
    """Take a name away from an item.

    Args:
        item_id: The item's id.

    Returns:
        The item after the change.

    Raises:
        ApiError: 409 ``alias_not_removable`` when the alias is the item's
            own name or not one of its aliases.
    """
    body = _Alias.model_validate(_body())
    try:
        HouseholdItemStore(_db_path()).remove_alias(item_id, body.alias, actor=g.actor)
    except ItemNotFoundError:
        raise
    except ValueError as err:
        raise ApiError(409, "alias_not_removable", str(err)) from err
    return _item_payload(_require_item(item_id), detailed=True), 200


@api_v2.post("/items/<int:item_id>/merge")
@mutation()
def merge_item(item_id: int) -> JsonResult:
    """Merge this item into another, reversibly.

    Any open replenishment needs the two items had are folded into one.

    Args:
        item_id: The item that goes away.

    Returns:
        The surviving item.
    """
    body = _MergeTarget.model_validate(_body())
    db_path = _db_path()
    HouseholdItemStore(db_path).merge(item_id, body.into_item_id, actor=g.actor)
    ReplenishmentNeeds(db_path).fold_merged_needs(body.into_item_id, actor=g.actor)
    return _item_payload(_require_item(body.into_item_id), detailed=True), 200


@api_v2.post("/items/<int:item_id>/unmerge")
@mutation()
def unmerge_item(item_id: int) -> JsonResult:
    """Undo a merge.

    Args:
        item_id: The merged item to restore.

    Returns:
        The restored item.
    """
    HouseholdItemStore(_db_path()).unmerge(item_id, actor=g.actor)
    return _item_payload(_require_item(item_id), detailed=True), 200


@api_v2.get("/items/<int:item_id>/history")
def item_history(item_id: int) -> Response:
    """Return why we believe what we believe about an item.

    Args:
        item_id: The item's id.

    Returns:
        The item's identity events and its full observation history,
        superseded rows and retractions included.
    """
    item = _require_item(item_id)
    db_path = _db_path()
    events = HouseholdItemStore(db_path).history(item.id)
    observations = InventoryLedger(db_path).history(item.id)
    return _json(
        {
            "item_id": item.id,
            "identity_events": [e.model_dump(mode="json") for e in events],
            "observations": [o.model_dump(mode="json") for o in observations],
        },
        200,
    )
