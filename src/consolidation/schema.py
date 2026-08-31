"""Persistence schema — the declarative target model, and nothing that runs.

Only SQLAlchemy Core ``Table`` metadata: the shape every other layer agrees on.
No function here takes a ``Connection``. Code that *executes* against the
database lives in :mod:`consolidation.repository` (aggregate access for the use
cases) and :mod:`consolidation.infrastructure` (download, feed, and the one-way
schema refactor).

The full target schema and the migration steps are specified in
``spec/data-profile.md#refactored-database``.

Depends on: SQLAlchemy Core only.
"""

from __future__ import annotations

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
