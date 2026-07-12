"""Unit tests for ``grocery_butler.network_guard``.

Covers the two pure functions that back the tailnet-only admission guard
(issue #62): CIDR-list parsing (:func:`parse_allowed_cidrs`) and the
source-IP admission check (:func:`is_trusted_source`). Integration of the
guard into the Flask app (env wiring, exemptions, response shape) is
covered separately in ``tests/test_app_boundary.py``.

This module does not exist yet, so collection itself is expected to fail
with ``ModuleNotFoundError`` until ``grocery_butler/network_guard.py`` is
implemented -- that is the intended Gate 1 RED state for issue #62.
"""

from __future__ import annotations

import ipaddress

import pytest

from grocery_butler.network_guard import is_trusted_source, parse_allowed_cidrs

#: The production default CIDR list: loopback + IPv6 loopback + Tailscale CGNAT.
#: RFC1918 private ranges are deliberately excluded from the default.
DEFAULT_CIDRS = "127.0.0.0/8,::1/128,100.64.0.0/10"


# ---------------------------------------------------------------------------
# parse_allowed_cidrs
# ---------------------------------------------------------------------------


class TestParseAllowedCidrs:
    """Tests for parsing a comma-separated CIDR list into network objects."""

    def test_parse_allowed_cidrs_default_string_returns_three_networks(self) -> None:
        """Test the production default string parses to three networks."""
        result = parse_allowed_cidrs(DEFAULT_CIDRS)

        assert result == [
            ipaddress.ip_network("127.0.0.0/8"),
            ipaddress.ip_network("::1/128"),
            ipaddress.ip_network("100.64.0.0/10"),
        ]

    def test_parse_allowed_cidrs_whitespace_around_entries_stripped(self) -> None:
        """Test surrounding whitespace around entries does not break parsing."""
        result = parse_allowed_cidrs("  127.0.0.0/8 , ::1/128  ")

        assert result == [
            ipaddress.ip_network("127.0.0.0/8"),
            ipaddress.ip_network("::1/128"),
        ]

    def test_parse_allowed_cidrs_empty_entries_skipped(self) -> None:
        """Test blank entries from double commas/trailing commas are skipped."""
        result = parse_allowed_cidrs("127.0.0.0/8,,   ,::1/128,")

        assert result == [
            ipaddress.ip_network("127.0.0.0/8"),
            ipaddress.ip_network("::1/128"),
        ]

    def test_parse_allowed_cidrs_empty_string_returns_empty_list(self) -> None:
        """Test an empty (or whitespace-only) raw string yields no networks."""
        assert parse_allowed_cidrs("") == []
        assert parse_allowed_cidrs("   ") == []

    def test_parse_allowed_cidrs_invalid_cidr_raises_value_error(self) -> None:
        """Test a malformed CIDR entry raises ValueError rather than being
        silently dropped (fail loud on operator misconfiguration).
        """
        with pytest.raises(ValueError):
            parse_allowed_cidrs("not-a-cidr")

    def test_parse_allowed_cidrs_out_of_range_octet_raises_value_error(self) -> None:
        """Test an out-of-range IPv4 octet in a CIDR entry raises ValueError."""
        with pytest.raises(ValueError):
            parse_allowed_cidrs("300.300.300.300/24")

    def test_parse_allowed_cidrs_one_bad_entry_among_good_raises_value_error(
        self,
    ) -> None:
        """Test one invalid entry among otherwise-valid entries still raises."""
        with pytest.raises(ValueError):
            parse_allowed_cidrs("127.0.0.0/8,garbage,::1/128")


# ---------------------------------------------------------------------------
# is_trusted_source
# ---------------------------------------------------------------------------


class TestIsTrustedSource:
    """Tests for the source-IP admission check against a parsed CIDR list."""

    @pytest.fixture()
    def allowed(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        """Return the parsed production-default allowed network list."""
        return parse_allowed_cidrs(DEFAULT_CIDRS)

    def test_is_trusted_source_loopback_v4_true(
        self, allowed: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
    ) -> None:
        """Test 127.0.0.1 (IPv4 loopback) is trusted under the default list."""
        assert is_trusted_source("127.0.0.1", allowed) is True

    def test_is_trusted_source_loopback_v6_true(
        self, allowed: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
    ) -> None:
        """Test ::1 (IPv6 loopback) is trusted under the default list."""
        assert is_trusted_source("::1", allowed) is True

    def test_is_trusted_source_cgnat_lower_bound_true(
        self, allowed: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
    ) -> None:
        """Test 100.64.0.0 (CGNAT range start) is trusted under the default."""
        assert is_trusted_source("100.64.0.0", allowed) is True

    def test_is_trusted_source_cgnat_upper_bound_true(
        self, allowed: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
    ) -> None:
        """Test 100.127.255.255 (CGNAT range end) is trusted under the default."""
        assert is_trusted_source("100.127.255.255", allowed) is True

    def test_is_trusted_source_just_past_cgnat_upper_bound_false(
        self, allowed: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
    ) -> None:
        """Test 100.128.0.0, one address past the CGNAT range, is rejected."""
        assert is_trusted_source("100.128.0.0", allowed) is False

    def test_is_trusted_source_just_before_cgnat_lower_bound_false(
        self, allowed: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
    ) -> None:
        """Test 100.63.255.255, one address before the CGNAT range, is rejected."""
        assert is_trusted_source("100.63.255.255", allowed) is False

    def test_is_trusted_source_public_ip_false(
        self, allowed: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
    ) -> None:
        """Test a public internet address (8.8.8.8) is rejected."""
        assert is_trusted_source("8.8.8.8", allowed) is False

    def test_is_trusted_source_rfc1918_private_address_false(
        self, allowed: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
    ) -> None:
        """Test RFC1918 private 10.0.0.1 is rejected under the default list.

        RFC1918 ranges are deliberately excluded from the default so a
        misconfigured office/home LAN is not accidentally trusted.
        """
        assert is_trusted_source("10.0.0.1", allowed) is False

    def test_is_trusted_source_none_remote_addr_false(
        self, allowed: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
    ) -> None:
        """Test a None remote_addr (fail-closed) is rejected."""
        assert is_trusted_source(None, allowed) is False

    def test_is_trusted_source_malformed_string_false(
        self, allowed: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
    ) -> None:
        """Test a malformed IP string is rejected rather than raising."""
        assert is_trusted_source("not-an-ip-address", allowed) is False

    def test_is_trusted_source_out_of_range_octet_string_false(
        self, allowed: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
    ) -> None:
        """Test an out-of-range IPv4 octet string is rejected rather than
        raising.
        """
        assert is_trusted_source("999.999.999.999", allowed) is False

    def test_is_trusted_source_empty_allowed_list_false(self) -> None:
        """Test any address is rejected when the allowed list is empty."""
        assert is_trusted_source("127.0.0.1", []) is False

    def test_is_trusted_source_ipv4_mapped_cgnat_true(
        self, allowed: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
    ) -> None:
        """Test an IPv4-mapped IPv6 CGNAT peer is trusted under the default.

        A dual-stack listener (bound to ``::`` with ``IPV6_V6ONLY`` off)
        reports IPv4 peers as IPv4-mapped IPv6 strings like
        ``::ffff:100.64.1.2``. The mapped form must be normalized to its
        IPv4 equivalent before the CIDR check, or legitimate tailnet
        peers would be false-rejected.
        """
        assert is_trusted_source("::ffff:100.64.1.2", allowed) is True

    def test_is_trusted_source_ipv4_mapped_loopback_true(
        self, allowed: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
    ) -> None:
        """Test an IPv4-mapped IPv6 loopback peer is trusted under the default."""
        assert is_trusted_source("::ffff:127.0.0.1", allowed) is True

    def test_is_trusted_source_ipv4_mapped_public_false(
        self, allowed: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
    ) -> None:
        """Test an IPv4-mapped IPv6 *public* peer is still rejected.

        Normalization must widen only availability (mapped trusted
        peers admitted), never trust (mapped public peers stay out).
        """
        assert is_trusted_source("::ffff:8.8.8.8", allowed) is False

    def test_is_trusted_source_ipv4_mapped_just_past_cgnat_false(
        self, allowed: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
    ) -> None:
        """Test the mapped form of the first post-CGNAT address is rejected."""
        assert is_trusted_source("::ffff:100.128.0.0", allowed) is False

    def test_is_trusted_source_plain_ipv6_non_loopback_false(
        self, allowed: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
    ) -> None:
        """Test a non-mapped, non-loopback IPv6 address is still rejected."""
        assert is_trusted_source("2001:db8::1", allowed) is False
