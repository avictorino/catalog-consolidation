"""The validated seller-feed entry (domain entity)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

STRING_FIELDS = ("Id", "SellerName", "Name", "Brand", "Category")


class ProductEntry(BaseModel):
    """One seller listing, validated without materializing the feed."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    Id: str
    SellerName: str
    Name: str
    Brand: str | None = None
    Category: str | None = None

    @field_validator("Id", "SellerName", "Name")
    @classmethod
    def require_non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("must not be empty")
        return value
