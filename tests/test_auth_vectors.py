"""The bearer token format, pinned by data (issue #232).

Every other test mints its token with the same code that verifies it, so
those tests show the server agrees with itself. OpenClaw mints tokens with
its own implementation. ``docs/api/auth-token-vectors.json`` fixes the
format as inputs and exact outputs that both sides can be run against: a
change to how a token is built fails here even though minting and
verifying still agree with each other.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from grocery_butler import auth_middleware
from grocery_butler.app import create_app
from grocery_butler.auth_middleware import (
    MAX_TOKEN_AGE_SECONDS,
    MAX_TOKEN_FUTURE_SKEW_SECONDS,
    SECRET_ENV_VAR,
    AuthError,
    RequestBinding,
    mint_token,
)

ROOT = Path(__file__).resolve().parent.parent
VECTORS_FILE = ROOT / "docs" / "api" / "auth-token-vectors.json"
VECTORS: dict[str, Any] = json.loads(VECTORS_FILE.read_text(encoding="utf-8"))
CASES: list[dict[str, str]] = VECTORS["cases"]
TIMESTAMP: int = VECTORS["timestamp"]
CALLER: str = VECTORS["caller_id"]

by_name = pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])


@pytest.fixture(autouse=True)
def vector_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sign and verify with the vectors' own test value."""
    monkeypatch.setenv(SECRET_ENV_VAR, VECTORS["hmac_key_utf8"])


def _binding(case: dict[str, str], **changes: str) -> RequestBinding:
    """Build the binding for a case, with any field replaced."""
    fields = {**case, **changes}
    return RequestBinding.of(
        fields["method"], fields["path"], fields["body_utf8"].encode("utf-8")
    )


class TestTheVectorsAreWhatTheServerMints:
    """Minting from a case's inputs gives exactly its token."""

    @by_name
    def test_minting_reproduces_the_token(self, case: dict[str, str]) -> None:
        """The same inputs, the same string, byte for byte."""
        assert mint_token(CALLER, _binding(case), now=TIMESTAMP) == case["token"]

    def test_the_file_documents_the_window_the_code_enforces(self) -> None:
        """The published window can't drift from the constants."""
        assert VECTORS["accepted_window_seconds"] == {
            "older_than_now": MAX_TOKEN_AGE_SECONDS,
            "newer_than_now": MAX_TOKEN_FUTURE_SKEW_SECONDS,
        }


class TestTheServerAcceptsExactlyTheVectors:
    """A case's token is good for its own request and nothing else."""

    @by_name
    def test_accepted_for_its_own_request(self, case: dict[str, str]) -> None:
        """Verified at the moment it was minted."""
        verified = auth_middleware._verify_token(
            case["token"], _binding(case), now=TIMESTAMP
        )

        assert verified == CALLER

    @by_name
    @pytest.mark.parametrize(
        "changes",
        [{"path": "/api/v2/members"}, {"body_utf8": '{"other": true}'}],
        ids=["another-path", "another-body"],
    )
    def test_refused_for_any_other_request(
        self, case: dict[str, str], changes: dict[str, str]
    ) -> None:
        """Bound to the path and to the body."""
        with pytest.raises(AuthError, match="bad signature"):
            auth_middleware._verify_token(
                case["token"], _binding(case, **changes), now=TIMESTAMP
            )

    @by_name
    def test_refused_for_another_method(self, case: dict[str, str]) -> None:
        """Bound to the method, compared in upper case."""
        other = "DELETE" if case["method"].upper() != "DELETE" else "GET"

        with pytest.raises(AuthError, match="bad signature"):
            auth_middleware._verify_token(
                case["token"], _binding(case, method=other), now=TIMESTAMP
            )

    @pytest.mark.parametrize(
        ("seconds_later", "reason"),
        [
            (MAX_TOKEN_AGE_SECONDS + 1, "token expired"),
            (-MAX_TOKEN_FUTURE_SKEW_SECONDS - 1, "token from future"),
        ],
        ids=["too-old", "too-new"],
    )
    def test_refused_outside_the_window(self, seconds_later: int, reason: str) -> None:
        """One second past either edge is refused."""
        case = CASES[0]

        with pytest.raises(AuthError, match=reason):
            auth_middleware._verify_token(
                case["token"], _binding(case), now=TIMESTAMP + seconds_later
            )

    @pytest.mark.parametrize(
        "seconds_later",
        [MAX_TOKEN_AGE_SECONDS, -MAX_TOKEN_FUTURE_SKEW_SECONDS],
        ids=["oldest-allowed", "newest-allowed"],
    )
    def test_accepted_at_the_edges_of_the_window(self, seconds_later: int) -> None:
        """Exactly on either edge is still good."""
        case = CASES[0]

        verified = auth_middleware._verify_token(
            case["token"], _binding(case), now=TIMESTAMP + seconds_later
        )

        assert verified == CALLER


class TestOverHttp:
    """The running app accepts a vector's token on a real request."""

    @by_name
    def test_the_app_authenticates_the_request(
        self, case: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sent as a caller would send it, query string and all.

        The query string is on the URL and is not part of what was signed.
        Any answer but 401 means the token was accepted; what the route
        then makes of the body is not this test's business.
        """
        monkeypatch.setattr(auth_middleware.time, "time", lambda: float(TIMESTAMP))
        app = create_app(db_path=str(tmp_path / "vectors.db"))
        url = case["path"] + (f"?{case['query']}" if case["query"] else "")

        response = app.test_client().open(
            url,
            method=case["method"].upper(),
            data=case["body_utf8"].encode("utf-8"),
            content_type="application/json",
            headers={
                "Authorization": f"Bearer {case['token']}",
                "X-Household-Member": "geoff",
                "Idempotency-Key": f"vector-{case['name']}",
            },
        )

        assert response.status_code != 401, response.get_data(as_text=True)

    def test_the_app_refuses_a_vector_token_on_another_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same token, pointed elsewhere, is a 401."""
        monkeypatch.setattr(auth_middleware.time, "time", lambda: float(TIMESTAMP))
        app = create_app(db_path=str(tmp_path / "vectors.db"))

        response = app.test_client().get(
            "/api/v2/members",
            headers={"Authorization": f"Bearer {CASES[0]['token']}"},
        )

        assert response.status_code == 401
