"""Declarative target schema (SQLAlchemy Core ``Table`` metadata), the id minter, and
the shared string ``normalize`` function.

This is the canonical shape of the refactored catalog, shared by the feed import
(``consolidation.repository``) and the Alembic ``target_metadata``. The migration steps
that build this shape from a legacy database are the ``0001`` revision itself
(``migrations/versions/0001_refactor_catalog.py``); the connection/transaction lifecycle
that runs them lives in ``consolidation.repository``.

The full target schema is specified in ``spec/data-profile.md#refactored-database``.
"""

from __future__ import annotations

import re
import unicodedata
import uuid

from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

TARGET_TABLES = ("Brand", "Category", "Product", "ProductCategory", "Seller", "SellerProduct")

metadata = MetaData()

Brand = Table(
    "Brand",
    metadata,
    Column("Id", String, primary_key=True),  # uuid4
    Column("Name", String, nullable=False, unique=True),  # capitalized normalized name
)

Category = Table(
    "Category",
    metadata,
    Column("Id", String, primary_key=True),  # uuid4
    Column("Name", String, nullable=False, unique=True),  # capitalized normalized name
)

Seller = Table(
    "Seller",
    metadata,
    Column("Id", String, primary_key=True),  # uuid4
    Column("Name", String, nullable=False, unique=True),
)

Product = Table(
    "Product",
    metadata,
    Column("Id", String, primary_key=True),  # uuid4
    Column("Name", String, nullable=False),
    Column("BrandId", String, ForeignKey("Brand.Id")),  # nullable
)

ProductCategory = Table(
    "ProductCategory",
    metadata,
    Column("ProductId", String, ForeignKey("Product.Id"), primary_key=True, nullable=False),
    Column("CategoryId", String, ForeignKey("Category.Id"), primary_key=True, nullable=False),
)
Index("ix_ProductCategory_CategoryId", ProductCategory.c.CategoryId)

SellerProduct = Table(
    "SellerProduct",
    metadata,
    Column("SellerId", String, ForeignKey("Seller.Id"), primary_key=True, nullable=False),
    Column("ProductId", String, ForeignKey("Product.Id"), primary_key=True, nullable=False),
    Column("ExternalSku", String, nullable=False),  # the feed entry's Id (opaque)
    UniqueConstraint("SellerId", "ExternalSku"),
)


def new_uuid() -> str:
    """A fresh ``uuid4`` as a 36-char string, minted in Python (no DB coordination)."""
    return str(uuid.uuid4())


# --------------------------------------------------------------------------- #
# Normalization (see spec/contract.md section 3). SQLite lower() is ASCII-only
# and cannot fold accents, so this is done in Python. One function, applied
# identically to catalog names, feed names, brand values and category values —
# and to the title-cased form persisted in Brand/Category/Seller.Name.
# --------------------------------------------------------------------------- #
# Quote / apostrophe / prime marks are removed with no replacement, so
# ``Levi's`` -> ``levis`` (matches catalog ``Levis``) and ``12.9''`` -> ``12 9``.
# straight ' ` "  |  curly single/double  |  prime / double-prime  |  acute  |  modifier apostrophe
_QUOTE_MARKS = "'`\"‘’“”′″´ʼ"  # noqa: RUF001 -- enumerating quote/apostrophe glyphs on purpose
_QUOTE_STRIP = {ord(ch): None for ch in _QUOTE_MARKS}
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize(value: str | None) -> str:
    """Return the normalized form of ``value`` (``""`` for ``None``/blank).

    1. Unicode NFKD, drop combining marks (accent folding).
    2. Lowercase.
    3. Strip quote and apostrophe marks (no replacement).
    4. Replace every run of remaining non-alphanumeric characters with a single space.
    5. Collapse whitespace, trim.

    Digits and decimal separators inside numbers become separate tokens: ``12.9"`` /
    ``12.9''`` / ``12.9`` all normalize to ``12 9``.
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    unquoted = without_marks.lower().translate(_QUOTE_STRIP)
    spaced = _NON_ALNUM.sub(" ", unquoted)
    return " ".join(spaced.split())
