"""Typed request and response for Instacart's shopping list link (ADR 0014).

These mirror the documented body of ``POST /idp/v1/products/products_link``
and nothing more. They never leave this package: the rest of the system
speaks in shopping plans and handoff results.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_LINK_DAYS = 365
SHOPPING_LIST = "shopping_list"


class _Strict(BaseModel):
    """A model that refuses fields the documentation doesn't name."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Measurement(_Strict):
    """One way of stating how much of an item is wanted."""

    quantity: float = Field(gt=0)
    unit: str = Field(min_length=1)


class Filters(_Strict):
    """Optional narrowing of the products matched for a line."""

    brand_filters: list[str] = Field(default_factory=list)
    health_filters: list[str] = Field(default_factory=list)


class LineItem(_Strict):
    """One thing to shop for.

    ``name`` is the search term and ``display_text`` is what the shopper
    sees. When ``upcs`` is set, Instacart ignores ``filters``.
    """

    name: str = Field(min_length=1)
    display_text: str | None = None
    line_item_measurements: list[Measurement] = Field(default_factory=list)
    upcs: list[str] = Field(default_factory=list)
    filters: Filters | None = None


class ProductsLinkRequest(_Strict):
    """The body sent to create a shopping list page.

    ``expires_in`` is always sent: shopping list links have no default
    expiry.
    """

    title: str = Field(min_length=1)
    line_items: list[LineItem] = Field(min_length=1)
    expires_in: int = Field(ge=1, le=MAX_LINK_DAYS)
    link_type: str = SHOPPING_LIST

    def body(self) -> dict[str, object]:
        """Render the request as the JSON body to send.

        Returns:
            The body, without fields that were left unset.
        """
        return self.model_dump(mode="json", exclude_none=True)


class ProductsLinkResponse(_Strict):
    """What Instacart returns: the link, and nothing else."""

    products_link_url: str

    @field_validator("products_link_url")
    @classmethod
    def _is_an_https_link(cls, value: str) -> str:
        """Refuse anything that isn't an HTTPS link.

        Args:
            value: The returned link.

        Returns:
            The link.

        Raises:
            ValueError: If it is empty or not HTTPS.
        """
        if not value.startswith("https://") or len(value) <= len("https://"):
            msg = "the provider did not return an HTTPS link"
            raise ValueError(msg)
        return value
