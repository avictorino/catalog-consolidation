"""Declarative target schema (SQLAlchemy Core ``Table`` metadata) and the id minter.

The canonical shape of the refactored catalog, shared by the SQLite adapter
(``consolidation.sqlite_store``) and the Alembic ``target_metadata``. The migration
steps that build this shape are the ``0001`` revision itself.

The full target schema is specified in ``spec/data-profile.md#refactored-database``.
"""

from __future__ import annotations

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
