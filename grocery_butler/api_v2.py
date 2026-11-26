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

import datetime as dt
import functools
import json
import logging
from typing import TYPE_CHECKING, Any

from flask import Blueprint, Response, current_app, g, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from werkzeug.exceptions import HTTPException

from grocery_butler.auth_middleware import require_bearer
from grocery_butler.constraints import (
    COMMON_ALLERGENS,
    ConstraintNotFoundError,
    HouseholdRules,
)
from grocery_butler.data_lifecycle import HouseholdData
from grocery_butler.fulfillment.base import Capability
from grocery_butler.fulfillment.registry import default_registry
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
from grocery_butler.inventory_ledger import (
    AlreadySupersededError,
    InventoryLedger,
    ObservationNotFoundError,
)
from grocery_butler.members import (
    InvalidMemberKeyError,
    MemberExistsError,
    MemberNotFoundError,
    MemberStore,
)
from grocery_butler.models import (
    BatchStatus,
    HardConstraintKind,
    HouseholdItemUpdate,
    IngredientCategory,
    NeedReasonKind,
    NeedStatus,
    NewObservation,
    NewProposal,
    ObservationSource,
    PlanMeal,
    ProposalReview,
    SoftPreferenceKind,
    StockState,
)
from grocery_butler.observation_batches import (
    BatchNotFoundError,
    BatchNotOpenError,
    InvalidReviewError,
    ObservationBatches,
    ReviewsMissingError,
)
from grocery_butler.replenishment import (
    InvalidNeedMoveError,
    NeedNotFoundError,
    ReplenishmentNeeds,
)
from grocery_butler.shopping_plans import (
    PlanNotFoundError,
    RecipeNotFoundError,
    ShoppingPlans,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from grocery_butler.models import HouseholdItem, ObservationBatch, ShoppingPlan

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
    ObservationNotFoundError: (404, "observation_not_found"),
    AlreadySupersededError: (409, "observation_superseded"),
    BatchNotFoundError: (404, "batch_not_found"),
    BatchNotOpenError: (409, "batch_not_open"),
    InvalidReviewError: (409, "review_invalid"),
    NeedNotFoundError: (404, "need_not_found"),
    InvalidNeedMoveError: (409, "need_move_not_allowed"),
    PlanNotFoundError: (404, "plan_not_found"),
    RecipeNotFoundError: (404, "recipe_not_found"),
    ConstraintNotFoundError: (404, "constraint_not_found"),
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


# ---------------------------------------------------------------------------
# Observations a member states directly
# ---------------------------------------------------------------------------


class _StatedObservation(BaseModel):
    """Body of the direct observation capabilities.

    Only for what a household member said themselves. What a model inferred
    goes through an observation batch instead.
    """

    model_config = ConfigDict(extra="forbid")

    state: StockState
    quantity: float | None = None
    unit: str | None = None
    observed_at: dt.datetime | None = None
    notes: str = ""


class _Retraction(BaseModel):
    """Body of ``POST /observations/<id>/retract``."""

    model_config = ConfigDict(extra="forbid")

    notes: str = ""


def _stated(item_id: int, body: _StatedObservation) -> NewObservation:
    """Turn a member's statement into a ledger observation.

    Args:
        item_id: The item the statement is about.
        body: What the member said.

    Returns:
        A human observation attributed to the acting member.
    """
    return NewObservation(
        item_id=item_id,
        state=body.state,
        source=ObservationSource.HUMAN,
        actor=g.actor,
        quantity=body.quantity,
        unit=body.unit,
        observed_at=body.observed_at,
        notes=body.notes,
    )


@api_v2.post("/items/<int:item_id>/observations")
@mutation()
def record_observation(item_id: int) -> JsonResult:
    """Record what a member says about an item, and sync its need.

    Args:
        item_id: The item's id.

    Returns:
        The stored observation, with 201.
    """
    body = _StatedObservation.model_validate(_body())
    canonical = HouseholdItemStore(_db_path()).canonical(item_id)
    stored = InventoryLedger(_db_path()).record(_stated(canonical.id, body))
    ReplenishmentNeeds(_db_path()).sync_item(canonical.id)
    return stored.model_dump(mode="json"), 201


@api_v2.post("/observations/<int:observation_id>/correct")
@mutation()
def correct_observation(observation_id: int) -> JsonResult:
    """Replace an observation with what should have been recorded.

    Args:
        observation_id: The observation that was wrong.

    Returns:
        The stored correction, with 201.
    """
    body = _StatedObservation.model_validate(_body())
    ledger = InventoryLedger(_db_path())
    wrong = ledger.get(observation_id)
    if wrong is None:
        raise ObservationNotFoundError(observation_id)
    stored = ledger.correct(observation_id, _stated(wrong.item_id, body))
    ReplenishmentNeeds(_db_path()).sync_item(stored.item_id)
    return stored.model_dump(mode="json"), 201


@api_v2.post("/observations/<int:observation_id>/retract")
@mutation()
def retract_observation(observation_id: int) -> JsonResult:
    """Withdraw an observation without stating anything in its place.

    Args:
        observation_id: The observation to withdraw.

    Returns:
        The stored retraction, with 201.
    """
    body = _Retraction.model_validate(_body())
    stored = InventoryLedger(_db_path()).retract(
        observation_id, actor=g.actor, notes=body.notes
    )
    ReplenishmentNeeds(_db_path()).sync_item(stored.item_id)
    return stored.model_dump(mode="json"), 201


# ---------------------------------------------------------------------------
# Observation batches: proposals a member reviews
# ---------------------------------------------------------------------------


class _NewBatch(BaseModel):
    """Body of ``POST /observation-batches``."""

    model_config = ConfigDict(extra="forbid")

    source: ObservationSource
    proposals: list[NewProposal]
    evidence_ref: str | None = None
    notes: str = ""
    observed_at: dt.datetime | None = None


class _BatchReview(BaseModel):
    """Body of ``POST /observation-batches/<id>/resolve``."""

    model_config = ConfigDict(extra="forbid")

    reviews: list[ProposalReview] = Field(default_factory=list)
    accept_high_confidence: bool = False


def _batch_payload(batch: ObservationBatch) -> dict[str, Any]:
    """Shape a batch, flagging the proposals that need a member's eye.

    Args:
        batch: The observation batch.

    Returns:
        The batch's JSON object; each proposal carries ``needs_review``.
    """
    payload: dict[str, Any] = batch.model_dump(mode="json")
    for shaped, proposal in zip(payload["proposals"], batch.proposals, strict=True):
        shaped["needs_review"] = proposal.needs_review
    return payload


@api_v2.post("/observation-batches")
@mutation()
def submit_batch() -> JsonResult:
    """Submit proposed observations for a member to review.

    Nothing is written to the observation ledger.

    Returns:
        The open batch, with 201.

    Raises:
        ApiError: 400 ``batch_invalid`` when the batch is empty, too large
            or from the ``system`` source.
    """
    body = _NewBatch.model_validate(_body())
    try:
        batch = ObservationBatches(_db_path()).submit(
            body.source,
            body.proposals,
            submitted_by=g.actor,
            evidence_ref=body.evidence_ref,
            notes=body.notes,
            observed_at=body.observed_at,
        )
    except ValueError as err:
        raise ApiError(400, "batch_invalid", str(err)) from err
    return _batch_payload(batch), 201


@api_v2.get("/observation-batches")
def list_batches() -> Response:
    """List observation batches, oldest first.

    Query: ``status`` filters by ``open``, ``confirmed``, ``rejected`` or
    ``expired``.

    Returns:
        ``{"batches": [...]}``.

    Raises:
        ApiError: 400 ``validation_failed`` for an unknown status.
    """
    raw = request.args.get("status")
    try:
        status = BatchStatus(raw) if raw else None
    except ValueError as err:
        raise ApiError(
            400, "validation_failed", "status must be a batch status."
        ) from err
    batches = ObservationBatches(_db_path()).list_batches(status)
    return _json({"batches": [_batch_payload(batch) for batch in batches]}, 200)


@api_v2.get("/observation-batches/<int:batch_id>")
def get_batch(batch_id: int) -> Response:
    """Return one batch with its proposals.

    Args:
        batch_id: The batch's id.

    Returns:
        The batch's JSON object.
    """
    batch = ObservationBatches(_db_path()).get(batch_id)
    if batch is None:
        raise BatchNotFoundError(batch_id)
    return _json(_batch_payload(batch), 200)


@api_v2.post("/observation-batches/<int:batch_id>/resolve")
@mutation()
def resolve_batch(batch_id: int) -> JsonResult:
    """Apply a member's review of a batch, all at once.

    Args:
        batch_id: The batch's id.

    Returns:
        The reviewed batch.

    Raises:
        ApiError: 409 ``reviews_missing`` when some proposals have no
            decision; ``details.proposal_ids`` lists them.
    """
    body = _BatchReview.model_validate(_body())
    try:
        batch = ObservationBatches(_db_path()).resolve(
            batch_id,
            body.reviews,
            actor=g.actor,
            accept_high_confidence=body.accept_high_confidence,
        )
    except ReviewsMissingError as err:
        raise ApiError(
            409, "reviews_missing", str(err), {"proposal_ids": err.proposal_ids}
        ) from err
    return _batch_payload(batch), 200


@api_v2.post("/observation-batches/<int:batch_id>/reject")
@mutation()
def reject_batch(batch_id: int) -> JsonResult:
    """Throw a whole batch away. Nothing is written to the ledger.

    Args:
        batch_id: The batch's id.

    Returns:
        The rejected batch.
    """
    batch = ObservationBatches(_db_path()).reject(batch_id, actor=g.actor)
    return _batch_payload(batch), 200


# ---------------------------------------------------------------------------
# Replenishment needs
# ---------------------------------------------------------------------------


class _NeedRequest(BaseModel):
    """Body of ``POST /needs``: a member asking for an item."""

    model_config = ConfigDict(extra="forbid")

    item_id: int
    note: str = ""


class _NeedMove(BaseModel):
    """Body of the need decisions that take only a note."""

    model_config = ConfigDict(extra="forbid")

    note: str = ""


class _NeedDeferral(BaseModel):
    """Body of ``POST /needs/<id>/defer``."""

    model_config = ConfigDict(extra="forbid")

    until: dt.datetime | None = None
    note: str = ""


def _need_payload(need_id: int) -> dict[str, Any]:
    """Shape a need with the history of its status changes.

    Args:
        need_id: The need's id.

    Returns:
        The need's JSON object, with ``history``.

    Raises:
        NeedNotFoundError: If the need does not exist.
    """
    needs = ReplenishmentNeeds(_db_path())
    need = needs.get(need_id)
    if need is None:
        raise NeedNotFoundError(need_id)
    payload: dict[str, Any] = need.model_dump(mode="json")
    payload["history"] = [
        event.model_dump(mode="json") for event in needs.history(need_id)
    ]
    return payload


def _move_need(need_id: int, to_status: NeedStatus) -> JsonResult:
    """Apply a member's decision that takes only a note.

    Args:
        need_id: The need's id.
        to_status: The status the decision moves it to.

    Returns:
        The need after the move.
    """
    body = _NeedMove.model_validate(request.get_json(silent=True) or {})
    ReplenishmentNeeds(_db_path()).transition(
        need_id, to_status, actor=g.actor, note=body.note
    )
    return _need_payload(need_id), 200


@api_v2.get("/needs")
def list_needs() -> Response:
    """List replenishment needs, oldest first.

    Query: ``status`` may be repeated to pick statuses. Defaults to every
    open status.

    Returns:
        ``{"needs": [...]}``.

    Raises:
        ApiError: 400 ``validation_failed`` for an unknown status.
    """
    raw = request.args.getlist("status")
    try:
        statuses = {NeedStatus(value) for value in raw} or None
    except ValueError as err:
        raise ApiError(
            400, "validation_failed", "status must be a replenishment need status."
        ) from err
    needs = ReplenishmentNeeds(_db_path()).list_needs(statuses)
    return _json({"needs": [need.model_dump(mode="json") for need in needs]}, 200)


@api_v2.post("/needs")
@mutation()
def request_need() -> JsonResult:
    """Ask for an item on a member's behalf.

    The request joins the item's open need, or starts one that is already
    approved (ADR 0008).

    Returns:
        The need, with 201.
    """
    body = _NeedRequest.model_validate(_body())
    need = ReplenishmentNeeds(_db_path()).add_reason(
        body.item_id,
        NeedReasonKind.MANUAL_REQUEST,
        source_key=f"member:{g.actor}",
        actor=g.actor,
        detail={"note": body.note},
    )
    return _need_payload(need.id), 201


@api_v2.get("/needs/<int:need_id>")
def get_need(need_id: int) -> Response:
    """Return one need with its reasons and history.

    Args:
        need_id: The need's id.

    Returns:
        The need's JSON object.
    """
    return _json(_need_payload(need_id), 200)


@api_v2.post("/needs/<int:need_id>/approve")
@mutation()
def approve_need(need_id: int) -> JsonResult:
    """Approve a need: the household wants this bought.

    Args:
        need_id: The need's id.

    Returns:
        The need after the move.
    """
    return _move_need(need_id, NeedStatus.APPROVED)


@api_v2.post("/needs/<int:need_id>/dismiss")
@mutation()
def dismiss_need(need_id: int) -> JsonResult:
    """Dismiss a need: don't suggest this again on the same evidence.

    Args:
        need_id: The need's id.

    Returns:
        The need after the move.
    """
    return _move_need(need_id, NeedStatus.DISMISSED)


@api_v2.post("/needs/<int:need_id>/cancel")
@mutation()
def cancel_need(need_id: int) -> JsonResult:
    """Cancel a need: it is no longer wanted.

    Args:
        need_id: The need's id.

    Returns:
        The need after the move.
    """
    return _move_need(need_id, NeedStatus.CANCELLED)


@api_v2.post("/needs/<int:need_id>/reopen")
@mutation()
def reopen_need(need_id: int) -> JsonResult:
    """End a deferral early and put the need back up for a decision.

    Args:
        need_id: The need's id.

    Returns:
        The need after the move.
    """
    return _move_need(need_id, NeedStatus.SUGGESTED)


@api_v2.post("/needs/<int:need_id>/defer")
@mutation()
def defer_need(need_id: int) -> JsonResult:
    """Snooze a need, until a date or until a member reopens it.

    Args:
        need_id: The need's id.

    Returns:
        The need after the move.
    """
    body = _NeedDeferral.model_validate(request.get_json(silent=True) or {})
    ReplenishmentNeeds(_db_path()).transition(
        need_id,
        NeedStatus.DEFERRED,
        actor=g.actor,
        note=body.note,
        deferred_until=body.until,
    )
    return _need_payload(need_id), 200


# ---------------------------------------------------------------------------
# Shopping plans
# ---------------------------------------------------------------------------


class _PlanRequest(BaseModel):
    """Body of ``POST /plans``."""

    model_config = ConfigDict(extra="forbid")

    meals: list[PlanMeal] = Field(default_factory=list)


def _plan_payload(plan: ShoppingPlan) -> dict[str, Any]:
    """Shape a stored plan, saying whether it still reflects the household.

    Args:
        plan: A stored plan version.

    Returns:
        The plan's JSON object. ``is_current`` is false once the needs,
        recipes or items under it have changed since it was built.
    """
    payload: dict[str, Any] = plan.model_dump(mode="json")
    fingerprint = ShoppingPlans(_db_path()).current_fingerprint(plan.version)
    payload["is_current"] = fingerprint == plan.content_hash
    return payload


@api_v2.post("/plans")
@mutation()
def build_plan() -> JsonResult:
    """Build the shopping plan from approved needs and the meals given.

    Returns:
        The plan: 201 when a new version was stored, 200 when nothing
        changed and the latest version still stands.

    Raises:
        ApiError: 400 ``plan_invalid`` when a recipe is listed twice.
    """
    body = _PlanRequest.model_validate(request.get_json(silent=True) or {})
    plans = ShoppingPlans(_db_path())
    before = plans.latest()
    try:
        plan = plans.build(body.meals, actor=g.actor)
    except ValueError as err:
        raise ApiError(400, "plan_invalid", str(err)) from err
    created = before is None or plan.version != before.version
    return _plan_payload(plan), 201 if created else 200


@api_v2.get("/plans")
def list_plans() -> Response:
    """List every plan version, oldest first, without their lines.

    Returns:
        ``{"plans": [...]}``, each with its version, hash, line count,
        author and time.
    """
    summaries = [
        {
            "version": plan.version,
            "content_hash": plan.content_hash,
            "line_count": len(plan.lines),
            "created_by": plan.created_by,
            "created_at": plan.created_at.isoformat(),
        }
        for plan in ShoppingPlans(_db_path()).list_plans()
    ]
    return _json({"plans": summaries}, 200)


@api_v2.get("/plans/latest")
def latest_plan() -> Response:
    """Return the current plan.

    Returns:
        The latest plan version with its lines.

    Raises:
        PlanNotFoundError: If no plan was ever built.
    """
    plan = ShoppingPlans(_db_path()).latest()
    if plan is None:
        raise PlanNotFoundError(1)
    return _json(_plan_payload(plan), 200)


@api_v2.get("/plans/<int:version>")
def get_plan(version: int) -> Response:
    """Return one plan version.

    Args:
        version: The plan version.

    Returns:
        The plan with its lines.

    Raises:
        PlanNotFoundError: If there is no such version.
    """
    plan = ShoppingPlans(_db_path()).get(version)
    if plan is None:
        raise PlanNotFoundError(version)
    return _json(_plan_payload(plan), 200)


# ---------------------------------------------------------------------------
# Household data: the full export
# ---------------------------------------------------------------------------


@api_v2.get("/household/export")
def export_household() -> Response:
    """Export every household table as one document (ADR 0013).

    The same document the ``data export`` command writes, and the format a
    restore reads. Reading it changes nothing. Deleting and restoring are
    not offered over HTTP: they are operator commands.

    Returns:
        The export: format, dialect, schema version, when it was taken,
        and every row of every household table.
    """
    return _json(HouseholdData(_db_path()).export(), 200)


# ---------------------------------------------------------------------------
# Fulfillment: adapters and the universal list export
# ---------------------------------------------------------------------------


@api_v2.get("/fulfillment/adapters")
def list_adapters() -> Response:
    """List the fulfillment adapters this deployment offers.

    Returns:
        ``{"adapters": [...]}``: each adapter's name, capabilities,
        authorization, data freshness and whether a person must act.
    """
    adapters = [
        {
            "name": description.name,
            "display_name": description.display_name,
            "capabilities": sorted(c.value for c in description.capabilities),
            "authorization": description.authorization.value,
            "data_freshness": description.data_freshness,
            "human_required": description.human_required,
            "enforces": sorted(kind.value for kind in description.enforces),
        }
        for description in (a.describe() for a in default_registry().adapters())
    ]
    return _json({"adapters": adapters}, 200)


@api_v2.get("/plans/<int:version>/export")
def export_plan(version: int) -> Response:
    """Export a plan version as a list for any store.

    Query: ``format`` is ``text`` (the default), ``json`` or ``csv``.
    Exporting changes nothing and contacts nobody, so it needs no approval.

    Args:
        version: The plan version.

    Returns:
        The export's content with the plan version and hash it came from.

    Raises:
        PlanNotFoundError: If there is no such version.
        ApiError: 400 ``validation_failed`` for an unknown format.
    """
    plan = ShoppingPlans(_db_path()).get(version)
    if plan is None:
        raise PlanNotFoundError(version)
    adapter = default_registry().get("export")
    adapter.require(Capability.EXPORT_LIST)
    result = adapter.create_handoff(plan)
    wanted = request.args.get("format", "text")
    if wanted not in result.artifacts:
        raise ApiError(
            400,
            "validation_failed",
            f"format must be one of: {', '.join(sorted(result.artifacts))}.",
        )
    return _json(
        {
            "adapter": result.adapter,
            "status": result.status.value,
            "plan_version": result.plan_version,
            "plan_content_hash": result.plan_content_hash,
            "format": wanted,
            "content": result.artifacts[wanted],
            "human_action": result.human_action,
            "unenforced_constraints": [
                {
                    "item_id": entry.item_id,
                    "display_name": entry.display_name,
                    "kind": entry.kind.value,
                    "value": entry.value,
                }
                for entry in result.unenforced_constraints
            ],
        },
        200,
    )


# ---------------------------------------------------------------------------
# Hard constraints and soft preferences
# ---------------------------------------------------------------------------


class _NewHardConstraint(BaseModel):
    """Body of ``POST /constraints``."""

    model_config = ConfigDict(extra="forbid")

    kind: HardConstraintKind
    value: str = ""
    item_id: int | None = None
    note: str = ""


class _NewSoftPreference(BaseModel):
    """Body of ``POST /soft-preferences``.

    ``kind`` is a soft preference kind. A hard constraint kind fails
    validation here: it can only be added through ``POST /constraints``.
    """

    model_config = ConfigDict(extra="forbid")

    kind: SoftPreferenceKind
    value: str = ""
    item_id: int | None = None


@api_v2.get("/constraints")
def list_constraints() -> Response:
    """List the household's hard constraints, oldest first.

    Query: ``include_inactive=true`` also returns removed constraints;
    ``item_id`` returns only those that apply to that item.

    Returns:
        ``{"constraints": [...], "common_allergens": [...]}``.
    """
    rules = HouseholdRules(_db_path())
    item_id = request.args.get("item_id", type=int)
    if item_id is not None:
        constraints = rules.hard_for_item(item_id)
    else:
        include = request.args.get("include_inactive") == "true"
        constraints = rules.list_hard(include_inactive=include)
    return _json(
        {
            "constraints": [
                {**c.model_dump(mode="json"), "scope": c.scope} for c in constraints
            ],
            "common_allergens": list(COMMON_ALLERGENS),
        },
        200,
    )


@api_v2.post("/constraints")
@mutation()
def add_constraint() -> JsonResult:
    """Add a hard constraint: something that must hold.

    Returns:
        The active constraint, with 201.

    Raises:
        ApiError: 400 ``constraint_invalid`` when an allergen or dietary
            rule has no value, or do-not-substitute names no item.
    """
    body = _NewHardConstraint.model_validate(_body())
    try:
        constraint = HouseholdRules(_db_path()).add_hard(
            body.kind,
            body.value,
            actor=g.actor,
            item_id=body.item_id,
            note=body.note,
        )
    except ValueError as err:
        raise ApiError(400, "constraint_invalid", str(err)) from err
    return {**constraint.model_dump(mode="json"), "scope": constraint.scope}, 201


@api_v2.post("/constraints/<int:constraint_id>/deactivate")
@mutation()
def deactivate_constraint(constraint_id: int) -> JsonResult:
    """Stop a hard constraint applying. Its record is kept.

    Args:
        constraint_id: The constraint's id.

    Returns:
        The deactivated constraint.
    """
    constraint = HouseholdRules(_db_path()).deactivate_hard(
        constraint_id, actor=g.actor
    )
    return {**constraint.model_dump(mode="json"), "scope": constraint.scope}, 200


@api_v2.get("/soft-preferences")
def list_soft_preferences() -> Response:
    """List soft preferences: what the household would like.

    Query: ``item_id`` returns everything that applies to that item (its
    own brand and size, matching brand rules, and stored preferences).
    Without it, the household-wide stored preferences are returned.

    Returns:
        ``{"preferences": [...]}``.
    """
    rules = HouseholdRules(_db_path())
    item_id = request.args.get("item_id", type=int)
    preferences = (
        rules.soft_for_item(item_id)
        if item_id is not None
        else rules.soft_for_household()
    )
    return _json({"preferences": [p.model_dump(mode="json") for p in preferences]}, 200)


@api_v2.post("/soft-preferences")
@mutation()
def add_soft_preference() -> JsonResult:
    """Store a soft preference: organic, generic, a brand or a size.

    Returns:
        What applies to the item (or the household) afterwards, with 201.
    """
    body = _NewSoftPreference.model_validate(_body())
    rules = HouseholdRules(_db_path())
    rules.add_soft(body.kind, body.value, actor=g.actor, item_id=body.item_id)
    preferences = (
        rules.soft_for_item(body.item_id)
        if body.item_id is not None
        else rules.soft_for_household()
    )
    return {"preferences": [p.model_dump(mode="json") for p in preferences]}, 201
