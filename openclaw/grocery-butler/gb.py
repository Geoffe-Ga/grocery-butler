#!/usr/bin/env python3
"""Call Grocery Butler's API with a correctly signed request.

Grocery Butler accepts a bearer token that is minted for one request and
bound to its method, path and exact body, so a fixed header can't be used.
This script mints the token and sends the request. It needs nothing but
Python's standard library, so it runs wherever OpenClaw runs.

    gb.py GET /api/v2/items
    gb.py GET '/api/v2/items?q=milk'
    gb.py POST /api/v2/needs --member geoff --data '{"item_id": 12}'

The response body is printed to standard output. The exit status is 0 for
a 2xx answer, 1 for any other answer (the error body is still printed),
2 when the call could not be made as asked, and 3 when Grocery Butler
could not be reached.

Settings, from the environment:

    GROCERY_BUTLER_URL        Where it listens. Default http://127.0.0.1:8000
    RUBOTPAUL_SHARED_SECRET   The signing secret. If unset, it is read from
                              the file named by GROCERY_BUTLER_ENV_FILE, or
                              from deploy/vps/.env in this checkout.
    GROCERY_BUTLER_CALLER     The caller name in the token. Default rubotpaul

The token format is fixed by docs/api/auth-token-vectors.json, and this
script is tested against those vectors.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_URL = "http://127.0.0.1:8000"
DEFAULT_CALLER = "rubotpaul"
SIGNING_VARIABLE = "RUBOTPAUL_SHARED_SECRET"
MEMBER_HEADER = "X-Household-Member"
IDEMPOTENCY_HEADER = "Idempotency-Key"
TIMEOUT_SECONDS = 30.0

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3

#: Methods that change something, and so need the two change headers.
CHANGING = frozenset({"POST", "PATCH", "DELETE"})


class UsageError(Exception):
    """The call can't be made as asked; the message says what to fix."""


@dataclass(frozen=True)
class SignedRequest:
    """One request, signed and ready to send."""

    secure: bool
    host: str
    method: str
    target: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


def mint_token(
    secret: str, caller: str, method: str, path: str, body: bytes, now: float
) -> str:
    """Mint the bearer token for one exact request.

    Args:
        secret: The shared signing secret.
        caller: The caller name to embed.
        method: The HTTP method, in any case.
        path: The request path, without its query string.
        body: The exact bytes that will be sent; empty for no body.
        now: The current time in seconds since the epoch.

    Returns:
        ``<caller>.<timestamp>.<hex HMAC-SHA256>``.
    """
    timestamp = int(now)
    signed = "\x00".join(
        (caller, str(timestamp), method.upper(), path, hashlib.sha256(body).hexdigest())
    ).encode()
    signature = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"{caller}.{timestamp}.{signature}"


def _secret_from_file(path: Path) -> str:
    """Read the signing secret from an env-style file.

    Args:
        path: A file of ``NAME=value`` lines.

    Returns:
        The secret, or an empty string if the file or the line is missing.
    """
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name.strip() == SIGNING_VARIABLE:
            return value.strip().strip("\"'")
    return ""


def find_secret(environ: dict[str, str]) -> str:
    """Find the signing secret without it ever being typed on a command line.

    Args:
        environ: The process environment.

    Returns:
        The secret.

    Raises:
        UsageError: If it is not in the environment or in the env file.
    """
    direct = environ.get(SIGNING_VARIABLE, "")
    if direct:
        return direct
    checkout = Path(__file__).resolve().parents[2]
    env_file = Path(
        environ.get("GROCERY_BUTLER_ENV_FILE") or checkout / "deploy" / "vps" / ".env"
    )
    found = _secret_from_file(env_file)
    if not found:
        msg = (
            f"No signing secret. Set {SIGNING_VARIABLE}, or point"
            f" GROCERY_BUTLER_ENV_FILE at a file that has it ({env_file} has none)."
        )
        raise UsageError(msg)
    return found


def _body_bytes(args: argparse.Namespace) -> bytes:
    """Return the exact bytes to send as the request body.

    Args:
        args: The parsed command line.

    Returns:
        The body, or empty bytes when none was given.
    """
    if args.data_file:
        return Path(args.data_file).read_bytes()
    return (args.data or "").encode("utf-8")


def _where(environ: dict[str, str]) -> tuple[bool, str]:
    """Work out where Grocery Butler listens.

    Args:
        environ: The process environment.

    Returns:
        Whether to use TLS, and the host with its port.

    Raises:
        UsageError: If GROCERY_BUTLER_URL is not an http or https address.
    """
    base = urlsplit(environ.get("GROCERY_BUTLER_URL", DEFAULT_URL))
    if base.scheme not in ("http", "https") or not base.netloc:
        msg = "GROCERY_BUTLER_URL must look like http://127.0.0.1:8000."
        raise UsageError(msg)
    return base.scheme == "https", base.netloc


def build_request(
    args: argparse.Namespace, environ: dict[str, str], now: float
) -> SignedRequest:
    """Build the signed request for a command line.

    Args:
        args: The parsed command line.
        environ: The process environment.
        now: The current time in seconds since the epoch.

    Returns:
        The request, ready to send.

    Raises:
        UsageError: If the path is not a path, the address is not usable,
            or no secret can be found.
    """
    method = args.method.upper()
    target = urlsplit(args.path)
    if target.scheme or target.netloc or not target.path.startswith("/"):
        msg = "PATH must be a path such as /api/v2/items, not a full URL."
        raise UsageError(msg)
    secure, host = _where(environ)
    body = _body_bytes(args)
    caller = environ.get("GROCERY_BUTLER_CALLER", DEFAULT_CALLER)
    token = mint_token(find_secret(environ), caller, method, target.path, body, now)
    headers = {"Authorization": f"Bearer {token}"}
    if body:
        headers["Content-Type"] = "application/json"
    if method in CHANGING:
        headers[IDEMPOTENCY_HEADER] = args.key or f"oc-{uuid.uuid4().hex}"
        if args.member:
            headers[MEMBER_HEADER] = args.member
    return SignedRequest(secure, host, method, args.path, body, headers)


def send(request: SignedRequest) -> tuple[int, str]:
    """Send a signed request.

    Args:
        request: The request.

    Returns:
        The HTTP status and the response body as text.

    Raises:
        OSError: If Grocery Butler can't be reached.
        http.client.HTTPException: If the answer is not valid HTTP.
    """
    connect = (
        http.client.HTTPSConnection if request.secure else http.client.HTTPConnection
    )
    connection = connect(request.host, timeout=TIMEOUT_SECONDS)
    try:
        connection.request(
            request.method,
            request.target,
            body=request.body or None,
            headers=request.headers,
        )
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8")
    finally:
        connection.close()


def _parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = argparse.ArgumentParser(
        prog="gb.py", description="Call Grocery Butler with a signed request."
    )
    parser.add_argument("method", help="GET, POST, PATCH or DELETE")
    parser.add_argument("path", help="For example /api/v2/items?q=milk")
    body = parser.add_mutually_exclusive_group()
    body.add_argument("--data", help="The JSON body, as text")
    body.add_argument("--data-file", help="A file holding the JSON body")
    parser.add_argument(
        "--member",
        help="The household member who is acting; required for a v2 change",
    )
    parser.add_argument(
        "--key",
        help="Idempotency key to reuse when repeating a change; made up if omitted",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Send one signed request and print the answer.

    Args:
        argv: The command line, without the program name.

    Returns:
        The exit status described in the module docstring.
    """
    args = _parser().parse_args(argv)
    try:
        request = build_request(args, dict(os.environ), time.time())
    except UsageError as err:
        print(f"gb: {err}", file=sys.stderr)
        return EXIT_USAGE
    if IDEMPOTENCY_HEADER in request.headers:
        key = request.headers[IDEMPOTENCY_HEADER]
        print(f"{IDEMPOTENCY_HEADER}: {key}", file=sys.stderr)
    try:
        status, text = send(request)
    except (OSError, http.client.HTTPException) as err:
        print(f"gb: could not reach Grocery Butler: {err}", file=sys.stderr)
        return EXIT_UNREACHABLE
    print(f"HTTP {status}", file=sys.stderr)
    print(text)
    return EXIT_OK if 200 <= status < 300 else EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
