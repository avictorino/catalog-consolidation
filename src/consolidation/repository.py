"""Repositories — the only code that touches the catalog database connection.

One repository per aggregate / reference table; :class:`CatalogRepositories`
bundles the five, reads the catalog, and owns the per-entry transaction. The
connection is private to this module: use cases call intention-revealing methods
(``get_or_create``, ``link``, ``load_catalog``, ``entry_transaction``), never
``conn.execute`` / ``conn.begin`` / ``conn.commit`` themselves.

Depends on: :mod:`consolidation.domain`, :mod:`consolidation.schema`,
SQLAlchemy Core.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

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
        self._conn = conn
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
        self._conn.execute(insert(self.table).values(Id=new_id, Name=normalized.title()))
        self._by_norm[normalized] = new_id
        return new_id


class BrandRepository(_ReferenceRepository):
    table = schema.Brand


class CategoryRepository(_ReferenceRepository):
    table = schema.Category


class SellerRepository:
    """Sellers are keyed on their exact submitted name (already whitespace-trimmed)."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn
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
        self._conn.execute(insert(schema.Seller).values(Id=new_id, Name=name))
        self._by_name[name] = new_id
        return new_id


class ProductRepository:
    """Reads the whole (small) catalog into a :class:`~consolidation.domain.Catalog`
    and persists new products and their category memberships."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def load_catalog(self) -> Catalog:
        """Read every product (with its brand and category display names) into the
        working :class:`Catalog`, then commit the read so the connection is idle
        and ready for the next entry transaction."""
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
        for product_id, name, brand, category in self._conn.execute(query):
            existing = rows.setdefault(product_id, (name, brand, []))
            if category and category not in existing[2]:
                existing[2].append(category)
        products = [
            Product(product_id, name, brand, tuple(categories))
            for product_id, (name, brand, categories) in rows.items()
        ]
        self._conn.commit()
        logger.info("catalog loaded products=%d", len(products))
        return Catalog(products)

    def add(self, product: Product, brand_id: str | None) -> None:
        self._conn.execute(
            insert(schema.Product).values(Id=product.id, Name=product.name, BrandId=brand_id)
        )

    def add_category_membership(self, product_id: str, category_id: str) -> None:
        self._conn.execute(
            insert(schema.ProductCategory)
            .values(ProductId=product_id, CategoryId=category_id)
            .prefix_with("OR IGNORE")
        )


class SellerListingRepository:
    """The ``SellerProduct`` junction: which seller offers which product, under
    which external SKU. Enforces ``UNIQUE (SellerId, ExternalSku)`` at the DB and
    exposes the reads a use case needs to keep that invariant intact."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def product_for_sku(self, seller_id: str, external_sku: str) -> str | None:
        return self._conn.execute(
            select(schema.SellerProduct.c.ProductId).where(
                schema.SellerProduct.c.SellerId == seller_id,
                schema.SellerProduct.c.ExternalSku == external_sku,
            )
        ).scalar_one_or_none()

    def sku_for_pair(self, seller_id: str, product_id: str) -> str | None:
        return self._conn.execute(
            select(schema.SellerProduct.c.ExternalSku).where(
                schema.SellerProduct.c.SellerId == seller_id,
                schema.SellerProduct.c.ProductId == product_id,
            )
        ).scalar_one_or_none()

    def link(self, seller_id: str, product_id: str, external_sku: str) -> bool:
        """Insert the ``(seller, product, sku)`` link. Returns ``True`` if a row
        was actually written (``False`` when it already existed)."""
        result = self._conn.execute(
            insert(schema.SellerProduct)
            .values(SellerId=seller_id, ProductId=product_id, ExternalSku=external_sku)
            .prefix_with("OR IGNORE")
        )
        return bool(result.rowcount)


class CatalogRepositories:
    """Every database access for one run, bound to a single connection.

    The connection stays private here — callers use the aggregate repositories,
    :meth:`load_catalog`, :meth:`entry_transaction` and :meth:`reload`.
    """

    def __init__(self, conn: Connection) -> None:
        self._conn = conn
        self._prime()

    def _prime(self) -> None:
        self.brands = BrandRepository(self._conn)
        self.categories = CategoryRepository(self._conn)
        self.sellers = SellerRepository(self._conn)
        self.products = ProductRepository(self._conn)
        self.listings = SellerListingRepository(self._conn)
        self._conn.commit()  # close the priming reads; connection left idle

    def reload(self) -> None:
        """Re-read every identity cache from the committed DB state — used after an
        entry transaction rolls back and its in-memory writes are gone."""
        self._prime()

    def load_catalog(self) -> Catalog:
        return self.products.load_catalog()

    @contextmanager
    def entry_transaction(self) -> Iterator[None]:
        """One transaction per feed entry: commit on success, roll back on error."""
        trans = self._conn.begin()
        try:
            yield
            trans.commit()
        except Exception:
            if trans.is_active:
                trans.rollback()
            raise
