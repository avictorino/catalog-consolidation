"""Repositories — the only code that reads and writes the catalog database.

One repository per aggregate / reference table. Each wraps a live SQLAlchemy
Core ``Connection``, keeps a small identity cache primed from the current
transaction, and exposes intention-revealing methods (``get_or_create``,
``link``, ``load_catalog``) instead of raw SQL. Use cases depend on these; they
never build a statement themselves.

Depends on: :mod:`consolidation.domain`, :mod:`consolidation.schema`,
SQLAlchemy Core.
"""

from __future__ import annotations

import logging

from sqlalchemy import insert, select
from sqlalchemy.engine import Connection

from consolidation import schema
from consolidation.domain import Catalog, Product, new_uuid, normalize

logger = logging.getLogger("consolidation")


class _ReferenceRepository:
    """Shared get-or-create for a ``(Id, Name)`` reference table keyed on the
    normalized name (``Brand``, ``Category``)."""

    table = None  # set by subclass

    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self._by_norm: dict[str, str] = {
            normalize(name): id_
            for id_, name in conn.execute(select(self.table.c.Id, self.table.c.Name))
            if normalize(name)
        }

    def get_or_create(self, raw_name: str | None) -> str | None:
        normalized = normalize(raw_name)
        if not normalized:
            return None
        existing = self._by_norm.get(normalized)
        if existing:
            return existing
        new_id = new_uuid()
        self.conn.execute(insert(self.table).values(Id=new_id, Name=normalized.title()))
        self._by_norm[normalized] = new_id
        return new_id


class BrandRepository(_ReferenceRepository):
    table = schema.Brand


class CategoryRepository(_ReferenceRepository):
    table = schema.Category


class SellerRepository:
    """Sellers are keyed on their exact submitted name (already whitespace-trimmed)."""

    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self._by_name: dict[str, str] = {
            name: id_
            for id_, name in conn.execute(select(schema.Seller.c.Id, schema.Seller.c.Name))
        }

    def id_for(self, name: str) -> str | None:
        return self._by_name.get(name)

    def get_or_create(self, name: str) -> str:
        existing = self._by_name.get(name)
        if existing:
            return existing
        new_id = new_uuid()
        self.conn.execute(insert(schema.Seller).values(Id=new_id, Name=name))
        self._by_name[name] = new_id
        return new_id


class ProductRepository:
    """Reads the whole (small) catalog into a :class:`~consolidation.domain.Catalog`
    and persists new products and their category memberships."""

    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    def load_catalog(self) -> Catalog:
        query = select(
            schema.Product.c.Id,
            schema.Product.c.Name,
            schema.Brand.c.Name.label("BrandName"),
            schema.Category.c.Name.label("CategoryName"),
        ).select_from(
            schema.Product.outerjoin(schema.Brand, schema.Product.c.BrandId == schema.Brand.c.Id)
            .outerjoin(
                schema.ProductCategory,
                schema.Product.c.Id == schema.ProductCategory.c.ProductId,
            )
            .outerjoin(
                schema.Category,
                schema.ProductCategory.c.CategoryId == schema.Category.c.Id,
            )
        )
        rows: dict[str, tuple[str, str | None, list[str]]] = {}
        for product_id, name, brand, category in self.conn.execute(query):
            existing = rows.setdefault(product_id, (name, brand, []))
            if category and category not in existing[2]:
                existing[2].append(category)
        products = [
            Product(product_id, name, brand, tuple(categories))
            for product_id, (name, brand, categories) in rows.items()
        ]
        logger.info("catalog loaded products=%d", len(products))
        return Catalog(products)

    def add(self, product: Product, brand_id: str | None) -> None:
        self.conn.execute(
            insert(schema.Product).values(Id=product.id, Name=product.name, BrandId=brand_id)
        )

    def add_category_membership(self, product_id: str, category_id: str) -> None:
        self.conn.execute(
            insert(schema.ProductCategory)
            .values(ProductId=product_id, CategoryId=category_id)
            .prefix_with("OR IGNORE")
        )


class SellerListingRepository:
    """The ``SellerProduct`` junction: which seller offers which product, under
    which external SKU. Enforces ``UNIQUE (SellerId, ExternalSku)`` at the DB and
    exposes the reads a use case needs to keep that invariant intact."""

    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    def product_for_sku(self, seller_id: str, external_sku: str) -> str | None:
        return self.conn.execute(
            select(schema.SellerProduct.c.ProductId).where(
                schema.SellerProduct.c.SellerId == seller_id,
                schema.SellerProduct.c.ExternalSku == external_sku,
            )
        ).scalar_one_or_none()

    def sku_for_pair(self, seller_id: str, product_id: str) -> str | None:
        return self.conn.execute(
            select(schema.SellerProduct.c.ExternalSku).where(
                schema.SellerProduct.c.SellerId == seller_id,
                schema.SellerProduct.c.ProductId == product_id,
            )
        ).scalar_one_or_none()

    def link(self, seller_id: str, product_id: str, external_sku: str) -> bool:
        """Insert the ``(seller, product, sku)`` link. Returns ``True`` if a row
        was actually written (``False`` when it already existed)."""
        result = self.conn.execute(
            insert(schema.SellerProduct)
            .values(SellerId=seller_id, ProductId=product_id, ExternalSku=external_sku)
            .prefix_with("OR IGNORE")
        )
        return bool(result.rowcount)


def load_catalog(conn: Connection) -> Catalog:
    """Convenience wrapper: read the catalog through :class:`ProductRepository`."""
    return ProductRepository(conn).load_catalog()
