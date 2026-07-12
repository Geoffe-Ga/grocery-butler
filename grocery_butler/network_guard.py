"""Tailnet-only network boundary guard (issue #62).

Grocery Butler is designed to be reached only over the Tailscale tailnet
(or from loopback, for local development). Historically nothing enforced
that assumption at the application layer -- Railway's public edge would
happily forward any request straight through to Flask, so a stray public
DNS record or a Railway domain guess was enough to reach every
state-mutating route. This module closes that hole with a single
``app.before_request`` hook that rejects requests whose *socket peer
address* falls outside an explicit allow-list of CIDR ranges.

SECURITY-CRITICAL: admission is keyed **only** on ``request.remote_addr``
(the actual TCP peer address Werkzeug sees), never on
``X-Forwarded-For`` or any other client-supplied header, and this module
does not install (and must never install) Werkzeug's ``ProxyFix``.
Railway's edge is a shared, multi-tenant proxy: any header it forwards
from the original client is attacker-controlled input. If a future
change trusts ``X-Forwarded-For`` (directly, or indirectly via
``ProxyFix`` rewriting ``remote_addr`` from that header) *without* also
verifying the request arrived through a proxy hop that itself
authenticates the header, a public request can simply set
``X-Forwarded-For: 127.0.0.1`` and walk right back through the hole this
module exists to close. If proxy trust is ever required, it must be
paired with cryptographic or network-level verification of the proxy
hop -- never bare header trust.

Configuration (read once, at ``register_network_guard`` call time):

- ``TAILNET_GUARD_ENABLED``: kill switch. The guard is active unless
  this is explicitly set to one of ``false``/``0``/``no``
  (case-insensitive). Unset means enabled -- the guard is fail-closed
  by default.
- ``TAILNET_GUARD_ALLOWED_CIDRS``: comma-separated CIDR list. When
  unset, defaults to loopback + IPv6 loopback + the Tailscale CGNAT
  range (``127.0.0.0/8,::1/128,100.64.0.0/10``). Setting this variable
  *replaces* the default list entirely; it does not extend it.

``/health`` and ``/healthz`` are always exempt (matched by Flask
endpoint name, not path) so Railway's own health checks -- which arrive
from Railway's infrastructure, not the tailnet -- keep working.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from flask import Flask
    from flask.typing import ResponseReturnValue

LOG = logging.getLogger("network_guard")

#: Environment variable that toggles the guard on or off.
ENABLED_ENV_VAR = "TAILNET_GUARD_ENABLED"

#: Environment variable holding the comma-separated allow-list override.
ALLOWED_CIDRS_ENV_VAR = "TAILNET_GUARD_ALLOWED_CIDRS"

#: Production default: loopback + IPv6 loopback + Tailscale CGNAT range.
#: RFC1918 private ranges are deliberately excluded so a misconfigured
#: office/home LAN is never accidentally trusted.
DEFAULT_ALLOWED_CIDRS = "127.0.0.0/8,::1/128,100.64.0.0/10"

#: Case-insensitive spellings of "disabled" accepted for the kill switch.
_DISABLING_VALUES = frozenset({"false", "0", "no"})

#: Flask endpoint names exempt from the guard regardless of source.
_EXEMPT_ENDPOINTS = frozenset({"health", "healthz"})

#: Union alias for the network types produced by ``ipaddress.ip_network``.
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_allowed_cidrs(raw: str) -> list[IPNetwork]:
    """Parse a comma-separated CIDR list into network objects.

    Surrounding whitespace around each entry is stripped, and blank
    entries produced by empty/trailing/doubled commas are skipped. An
    empty (or whitespace-only) input yields an empty list rather than
    raising, since that is a legitimate "trust nothing" configuration.

    Args:
        raw: Comma-separated CIDR string, e.g. ``"127.0.0.0/8,::1/128"``.

    Returns:
        List of parsed IPv4/IPv6 network objects, in input order.

    Raises:
        ValueError: If any non-empty entry is not a valid CIDR network.
            This fails loud rather than silently dropping a
            misconfigured entry, since a dropped entry could
            unintentionally narrow (or, if it were the only entry,
            fully disable) the allow-list.
    """
    networks: list[IPNetwork] = []
    for entry in raw.split(","):
        stripped = entry.strip()
        if not stripped:
            continue
        networks.append(ipaddress.ip_network(stripped))
    return networks


def is_trusted_source(remote_addr: str | None, allowed: Sequence[IPNetwork]) -> bool:
    """Check whether a source address is within an allowed CIDR list.

    Fails closed: a missing address, a malformed address string, or an
    empty allow-list are all treated as untrusted rather than raising.

    IPv4-mapped IPv6 addresses (``::ffff:a.b.c.d``, as reported by a
    dual-stack listener for an IPv4 peer) are additionally checked in
    their unmapped IPv4 form, so a legitimate ``::ffff:100.64.1.2``
    tailnet peer is not false-rejected by the exact-version CIDR match.
    This only widens availability, never trust: the unmapped form is
    the *same* socket peer, and a mapped public address stays outside
    the allow-list either way.

    Args:
        remote_addr: The socket peer address to check, or ``None`` if
            unavailable.
        allowed: Parsed networks to check membership against.

    Returns:
        ``True`` if ``remote_addr`` is a valid address contained in at
        least one network in ``allowed`` (directly, or via its unmapped
        IPv4 form for IPv4-mapped IPv6 addresses), ``False`` otherwise.
    """
    if remote_addr is None:
        return False
    try:
        address = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [address]
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        candidates.append(address.ipv4_mapped)
    return any(candidate in network for candidate in candidates for network in allowed)


def _is_guard_enabled() -> bool:
    """Return whether the guard should be active, per the kill switch.

    Returns:
        ``False`` only if ``TAILNET_GUARD_ENABLED`` is explicitly set to
        one of ``false``/``0``/``no`` (case-insensitive); ``True``
        otherwise, including when the variable is unset.
    """
    raw = os.environ.get(ENABLED_ENV_VAR, "true")
    return raw.strip().lower() not in _DISABLING_VALUES


def _resolve_allowed_networks() -> list[IPNetwork]:
    """Resolve the allow-list from the environment at registration time.

    Returns:
        Parsed networks from ``TAILNET_GUARD_ALLOWED_CIDRS`` if set
        (replacing the default entirely), otherwise the parsed
        production default.
    """
    raw = os.environ.get(ALLOWED_CIDRS_ENV_VAR, DEFAULT_ALLOWED_CIDRS)
    return parse_allowed_cidrs(raw)


def register_network_guard(app: Flask) -> None:
    """Install the tailnet boundary guard on a Flask application.

    Reads ``TAILNET_GUARD_ENABLED`` and ``TAILNET_GUARD_ALLOWED_CIDRS``
    from the environment once, at registration time, and -- unless the
    kill switch disables it -- installs an ``app.before_request`` hook
    that rejects any request whose ``request.remote_addr`` is not in the
    resolved allow-list, except for the ``health``/``healthz``
    endpoints, which are always reachable.

    Rejected requests get a 403: a JSON ``{"error": "forbidden"}`` body
    for paths under ``/api/``, and the app's standard HTML error page
    everywhere else. Every rejection is logged at warning level with the
    request path and remote address (no headers, no bodies, no secrets).

    Startup observability: when active, the resolved allow-list CIDRs
    are logged at info level (``network_guard_enabled``) so operators
    can verify the live configuration from the deploy logs. If the
    resolved allow-list is *empty* (an explicitly-blank
    ``TAILNET_GUARD_ALLOWED_CIDRS``), a warning
    (``network_guard_empty_allowlist``) is logged instead, since that
    trust-nothing configuration locks out every non-health request.

    Args:
        app: Flask application instance to guard.
    """
    if not _is_guard_enabled():
        LOG.info("network_guard_disabled")
        return

    allowed = _resolve_allowed_networks()
    if allowed:
        LOG.info(
            "network_guard_enabled allowed_cidrs=%s",
            ",".join(str(network) for network in allowed),
        )
    else:
        LOG.warning(
            "network_guard_empty_allowlist -- resolved allow-list is empty; "
            "every non-health request will be rejected (403). If this is "
            "not intentional, unset %s entirely (do not set it to an empty "
            "string) to restore the default allow-list.",
            ALLOWED_CIDRS_ENV_VAR,
        )

    from flask import jsonify, render_template, request

    @app.before_request
    def _enforce_tailnet_boundary() -> ResponseReturnValue | None:
        """Reject any non-exempt request from outside the allow-list.

        Returns:
            ``None`` to let Flask continue normal routing when the
            request is exempt or trusted; otherwise a ``(body, 403)``
            response tuple.
        """
        if request.endpoint in _EXEMPT_ENDPOINTS:
            return None
        if is_trusted_source(request.remote_addr, allowed):
            return None

        LOG.warning(
            "network_guard_rejected path=%s remote_addr=%s",
            request.path,
            request.remote_addr,
        )
        if request.path.startswith("/api/"):
            return jsonify({"error": "forbidden"}), 403
        return render_template("error.html", code=403, message="Forbidden"), 403
