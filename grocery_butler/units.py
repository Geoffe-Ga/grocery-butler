"""Unit dimension classification, conversion, and product-size parsing.

Introduced to fix issue #59: :func:`grocery_butler.cart_builder._calculate_quantity`
used to divide a shopping-list quantity by the leading number of a product's
size string with no regard for units, causing gross over-orders (500 g vs.
"5 lb" ordering 100 units) and silent under-orders. This module provides the
unit-aware primitives that make quantity calculations dimensionally sound:

* :func:`dimension_of` classifies a :class:`~grocery_butler.models.Unit` into
  a physical :class:`Dimension` (mass, volume, or count), or ``None`` for
  packaging/other units that have no fixed physical size.
* :func:`convert` converts a quantity between two units of the same
  dimension, returning ``None`` when the units are incomparable.
* :func:`parse_size` strictly parses a product size string (e.g. ``"2 lb"``)
  into a ``(quantity, Unit)`` pair, returning ``None`` for anything it can't
  confidently resolve -- unlike
  :func:`grocery_butler.models.parse_unit`, it never falls back to
  :attr:`~grocery_butler.models.Unit.EACH` for unrecognized unit tokens.
"""

from __future__ import annotations

import re
from enum import StrEnum

from grocery_butler.models import _UNIT_ALIASES, Unit

# ---------------------------------------------------------------------------
# Dimension classification
# ---------------------------------------------------------------------------


class Dimension(StrEnum):
    """Physical dimension used to validate cross-unit conversions."""

    MASS = "mass"
    VOLUME = "volume"
    COUNT = "count"


# Mass units, expressed as grams-per-unit.
_MASS_FACTORS_G: dict[Unit, float] = {
    Unit.G: 1.0,
    Unit.KG: 1000.0,
    Unit.OZ: 28.349523125,
    Unit.LB: 453.59237,
}

# Volume units, expressed as milliliters-per-unit.
_VOLUME_FACTORS_ML: dict[Unit, float] = {
    Unit.ML: 1.0,
    Unit.L: 1000.0,
    Unit.TSP: 4.92892159,
    Unit.TBSP: 14.7867648,
    Unit.FL_OZ: 29.5735296,
    Unit.CUP: 236.588237,
    Unit.GAL: 3785.411784,
    Unit.PINT: 473.176473,
    Unit.QUART: 946.352946,
}

# Count units, expressed as each-per-unit.
_COUNT_FACTORS_EACH: dict[Unit, float] = {
    Unit.EACH: 1.0,
    Unit.DOZEN: 12.0,
}

# Packaging/"other" units (bag, can, box, jar, bottle, package, block, stick,
# pinch, dash, to_taste, bunch, head, clove, slice) are deliberately absent:
# they carry no fixed physical size, so they are non-convertible and
# classify as dimensionless (``dimension_of`` returns ``None``).

_DIMENSION_TABLE: dict[Unit, tuple[Dimension, float]] = {
    **{unit: (Dimension.MASS, factor) for unit, factor in _MASS_FACTORS_G.items()},
    **{unit: (Dimension.VOLUME, factor) for unit, factor in _VOLUME_FACTORS_ML.items()},
    **{unit: (Dimension.COUNT, factor) for unit, factor in _COUNT_FACTORS_EACH.items()},
}


def dimension_of(unit: Unit) -> Dimension | None:
    """Classify a unit's physical dimension.

    Args:
        unit: The unit to classify.

    Returns:
        The unit's ``Dimension`` (mass, volume, or count), or ``None`` if
        the unit is a packaging or "other" unit with no fixed physical size.
    """
    entry = _DIMENSION_TABLE.get(unit)
    return entry[0] if entry is not None else None


def convert(quantity: float, from_unit: Unit, to_unit: Unit) -> float | None:
    """Convert a quantity from one unit to another.

    Args:
        quantity: The numeric quantity in ``from_unit``.
        from_unit: The unit ``quantity`` is currently expressed in.
        to_unit: The unit to convert to.

    Returns:
        The converted quantity, or ``None`` if the two units are not
        comparable (different dimensions, or either unit is non-convertible).
        Converting a unit to itself always returns ``quantity`` unchanged.
    """
    if from_unit == to_unit:
        return quantity

    from_entry = _DIMENSION_TABLE.get(from_unit)
    to_entry = _DIMENSION_TABLE.get(to_unit)
    if from_entry is None or to_entry is None:
        return None

    from_dimension, from_factor = from_entry
    to_dimension, to_factor = to_entry
    if from_dimension != to_dimension:
        return None

    base_quantity = quantity * from_factor
    return base_quantity / to_factor


# ---------------------------------------------------------------------------
# Strict product-size parsing
# ---------------------------------------------------------------------------

_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*(.*)$")

# Unit tokens with internal spaces or punctuation that don't survive a plain
# ``Unit(...)`` or ``_UNIT_ALIASES`` lookup once normalized (periods removed,
# whitespace collapsed).
_EXTRA_SIZE_UNIT_ALIASES: dict[str, Unit] = {
    "fl oz": Unit.FL_OZ,
    "fl_oz": Unit.FL_OZ,
    "floz": Unit.FL_OZ,
}


def _normalize_unit_token(token: str) -> str:
    """Normalize a raw unit token for strict lookup.

    Strips surrounding whitespace, lower-cases, removes periods (so
    ``"fl. oz."`` and ``"fl oz"`` normalize the same), and collapses
    internal whitespace runs.

    Args:
        token: Raw unit token text.

    Returns:
        Normalized token text.
    """
    cleaned = token.strip().lower().replace(".", "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _resolve_unit_token(token: str) -> Unit | None:
    """Strictly resolve a unit token to a ``Unit`` enum member.

    Unlike :func:`grocery_butler.models.parse_unit`, this never falls back
    to ``Unit.EACH`` for an unrecognized token -- a miss returns ``None`` so
    callers can flag the size as unparseable instead of silently guessing.

    Args:
        token: Raw unit token text (e.g. ``"lb"``, ``"fl oz"``).

    Returns:
        The resolved ``Unit``, or ``None`` if the token isn't recognized.
    """
    normalized = _normalize_unit_token(token)
    if not normalized:
        return None
    try:
        return Unit(normalized)
    except ValueError:
        pass
    alias = _UNIT_ALIASES.get(normalized)
    if alias is not None:
        return alias
    return _EXTRA_SIZE_UNIT_ALIASES.get(normalized)


def parse_size(size: str) -> tuple[float, Unit] | None:
    """Strictly parse a product size string into a quantity and unit.

    Parses a leading numeric quantity followed by an optional unit token
    (e.g. ``"2 lb"``, ``"32 fl oz"``, ``"1.5 l"``). A bare number with no
    unit token (e.g. ``"12"``) is treated as a count of ``Unit.EACH``.

    This is a strict parser: any string with no leading number, or with an
    unrecognized unit token, returns ``None`` rather than guessing. This is
    the key behavioral difference from
    :func:`grocery_butler.models.parse_unit`, which falls back to
    ``Unit.EACH`` for unrecognized unit strings.

    Args:
        size: Raw product size string, e.g. ``"2 lb"`` or ``"16 oz"``.

    Returns:
        A ``(quantity, Unit)`` tuple, or ``None`` if the size string can't
        be confidently parsed.
    """
    match = _SIZE_RE.match(size)
    if not match:
        return None

    try:
        quantity = float(match.group(1))
    except ValueError:
        return None

    unit_token = match.group(2).strip()
    if not unit_token:
        return (quantity, Unit.EACH)

    unit = _resolve_unit_token(unit_token)
    if unit is None:
        return None

    return (quantity, unit)
