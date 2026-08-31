"""``CatalogRepository`` — the single object that owns the downloaded database.

Every interaction with the SQLite copy goes through this module: the engine/connection
lifecycle, source classification, the Alembic-driven schema refactor, foreign-key
enforcement, reading the catalog into memory (``load_catalog``), and applying one
validated feed entry at a time (``FeedImporter``). ``consolidation.pipeline`` composes
``CatalogRepository`` through the ``Catalog`` protocol and never imports SQLAlchemy or
Alembic itself; the pure matching logic lives in ``consolidation.resolver``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Literal, Protocol

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, insert, inspect, select
from sqlalchemy.engine import Connection, Engine

from consolidation import schema
from consolidation.feed import ProductEntry, Report, screen_entry
from consolidation.resolver import CatalogIndex, CatalogProduct, resolve_product
from consolidation.similarity import Similarity, build_similarity
from consolidation.util import normalize

logger = logging.getLogger("consolidation")

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

SourceKind = Literal["legacy", "migrated", "unrecognized"]


# --------------------------------------------------------------------------- #
# Reading the catalog into memory
# --------------------------------------------------------------------------- #
def load_catalog(conn: Connection) -> CatalogIndex:
    """Read the target catalog once; fuzzy retrieval remains a plain product scan."""
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
    product_data: dict[str, tuple[str, str | None, list[str]]] = {}
    for product_id, name, brand, category in conn.execute(query):
        existing = product_data.setdefault(product_id, (name, brand, []))
        if category and category not in existing[2]:
            existing[2].append(category)
    products = [
        CatalogProduct(product_id, name, brand, tuple(categories))
        for product_id, (name, brand, categories) in product_data.items()
    ]
    logger.info("catalog loaded products=%d", len(products))
    return CatalogIndex(products)


# --------------------------------------------------------------------------- #
# Persisting one feed entry
# --------------------------------------------------------------------------- #
class FeedImporter:
    """Apply one validated feed entry at a time inside the caller's transaction."""

    def __init__(
        self,
        conn: Connection,
        catalog: CatalogIndex,
        similarity: Similarity,
        threshold: float,
    ) -> None:
        self.conn = conn
        self.catalog = catalog
        self.similarity = similarity
        self.threshold = threshold
        self.brand_ids = self._load_reference_ids(schema.Brand)
        self.category_ids = self._load_reference_ids(schema.Category)
        self.seller_ids = self._load_seller_ids()

    def _load_reference_ids(self, table) -> dict[str, str]:
        return {
            normalize(name): id
            for id, name in self.conn.execute(select(table.c.Id, table.c.Name))
            if normalize(name)
        }

    def _load_seller_ids(self) -> dict[str, str]:
        return {
            name: id
            for id, name in self.conn.execute(select(schema.Seller.c.Id, schema.Seller.c.Name))
        }

    def _reference_id(self, table, cache: dict[str, str], raw_name: str | None) -> str | None:
        normalized = normalize(raw_name)
        if not normalized:
            return None
        existing = cache.get(normalized)
        if existing:
            return existing
        new_id = schema.new_uuid()
        self.conn.execute(insert(table).values(Id=new_id, Name=normalized.title()))
        cache[normalized] = new_id
        return new_id

    def _seller_id(self, seller_name: str) -> str:
        existing = self.seller_ids.get(seller_name)
        if existing:
            return existing
        new_id = schema.new_uuid()
        self.conn.execute(insert(schema.Seller).values(Id=new_id, Name=seller_name))
        self.seller_ids[seller_name] = new_id
        return new_id

    def _insert_product(self, entry: ProductEntry) -> CatalogProduct:
        brand_id = self._reference_id(schema.Brand, self.brand_ids, entry.Brand)
        product = CatalogProduct(schema.new_uuid(), entry.Name, entry.Brand)
        self.conn.execute(
            insert(schema.Product).values(
                Id=product.id,
                Name=product.name,
                BrandId=brand_id,
            )
        )
        self._attach_category(product, entry.Category)
        self.catalog.add(product)
        return product

    def _attach_category(self, product: CatalogProduct, raw_category: str | None) -> None:
        category_id = self._reference_id(schema.Category, self.category_ids, raw_category)
        normalized = normalize(raw_category)
        if not category_id or not normalized:
            return
        self.conn.execute(
            insert(schema.ProductCategory)
            .values(ProductId=product.id, CategoryId=category_id)
            .prefix_with("OR IGNORE")
        )
        if not any(normalize(category) == normalized for category in product.categories):
            product.categories = (*product.categories, normalized.title())

    def _existing_sku_product(self, seller_id: str, external_sku: str) -> str | None:
        return self.conn.execute(
            select(schema.SellerProduct.c.ProductId).where(
                schema.SellerProduct.c.SellerId == seller_id,
                schema.SellerProduct.c.ExternalSku == external_sku,
            )
        ).scalar_one_or_none()

    def _link(
        self,
        seller_id: str,
        product: CatalogProduct,
        entry: ProductEntry,
        record_index: int,
        report: Report,
    ) -> None:
        existing_sku_product = self._existing_sku_product(seller_id, entry.Id)
        if existing_sku_product and existing_sku_product != product.id:
            report.skipped += 1
            report.skipped_entries.append(
                {"record_index": record_index, "reason": "external SKU conflict"}
            )
            logger.warning("event=skip record=%d reason=external_sku_conflict", record_index)
            return

        existing_pair = self.conn.execute(
            select(schema.SellerProduct.c.ExternalSku).where(
                schema.SellerProduct.c.SellerId == seller_id,
                schema.SellerProduct.c.ProductId == product.id,
            )
        ).scalar_one_or_none()
        if existing_pair is not None:
            if existing_pair != entry.Id:
                logger.info(
                    "event=duplicate_listing record=%d seller_id=%s product_id=%s",
                    record_index,
                    seller_id,
                    product.id,
                )
            return

        statement = insert(schema.SellerProduct).values(
            SellerId=seller_id,
            ProductId=product.id,
            ExternalSku=entry.Id,
        )
        result = self.conn.execute(statement.prefix_with("OR IGNORE"))
        if result.rowcount:
            report.linked += 1

    def process(self, entry: ProductEntry, record_index: int, report: Report) -> None:
        if not screen_entry(entry, record_index, report):
            return

        product, reason, score = resolve_product(
            self.catalog, entry, self.similarity, self.threshold
        )
        if reason:
            report.skipped += 1
            report.skipped_entries.append({"record_index": record_index, "reason": reason})
            logger.warning("event=skip record=%d reason=%s", record_index, reason.replace(" ", "_"))
            return

        seller_id = self.seller_ids.get(entry.SellerName)
        if product is None:
            existing_sku_product = (
                self._existing_sku_product(seller_id, entry.Id) if seller_id else None
            )
            if existing_sku_product:
                product = next(
                    (
                        candidate
                        for candidate in self.catalog.products
                        if candidate.id == existing_sku_product
                    ),
                    None,
                )
                if product is None:
                    report.skipped += 1
                    report.skipped_entries.append(
                        {"record_index": record_index, "reason": "external SKU conflict"}
                    )
                    logger.warning(
                        "event=skip record=%d reason=external_sku_conflict", record_index
                    )
                    return
            else:
                product = self._insert_product(entry)
                report.new += 1
        elif score is not None:
            logger.warning(
                "event=approximate_match record=%d product_id=%s score=%.3f",
                record_index,
                product.id,
                score,
            )

        incoming_category = normalize(entry.Category)
        category_diverges = (
            bool(product.categories)
            and bool(incoming_category)
            and not any(normalize(category) == incoming_category for category in product.categories)
        )
        self._attach_category(product, entry.Category)
        if category_diverges:
            logger.warning(
                "event=category_divergence record=%d product_id=%s",
                record_index,
                product.id,
            )

        seller_id = seller_id or self._seller_id(entry.SellerName)
        self._link(seller_id, product, entry, record_index, report)


# --------------------------------------------------------------------------- #
# The repository
# --------------------------------------------------------------------------- #
class Catalog(Protocol):
    """The database surface ``pipeline.run`` depends on (a seam for tests)."""

    def __enter__(self) -> Catalog: ...

    def __exit__(self, *exc: object) -> None: ...

    def classify_source(self) -> SourceKind: ...

    def migrate(self) -> None: ...

    def enable_foreign_keys(self) -> None: ...

    def prepare_import(self, *, matcher: str, threshold: float) -> None: ...

    def item_transaction(self) -> AbstractContextManager[FeedImporter]: ...


class CatalogRepository:
    """Own one SQLite connection for the lifetime of a single consolidation run."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._engine: Engine | None = None
        self._conn: Connection | None = None
        self._similarity: Similarity | None = None
        self._threshold = 0.0
        self._importer: FeedImporter | None = None

    # ----------------------------------------------------------------- #
    # Lifecycle
    # ----------------------------------------------------------------- #
    def __enter__(self) -> CatalogRepository:
        self._engine = create_engine(f"sqlite:///{self._db_path}")
        self._conn = self._engine.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            if self._conn is not None:
                if self._conn.in_transaction():
                    self._conn.rollback()
                self._conn.close()
        finally:
            if self._engine is not None:
                self._engine.dispose()
            self._conn = None
            self._engine = None
            self._importer = None

    @property
    def _connection(self) -> Connection:
        if self._conn is None:
            raise RuntimeError("CatalogRepository must be used as a context manager")
        return self._conn

    # ----------------------------------------------------------------- #
    # Schema
    # ----------------------------------------------------------------- #
    def classify_source(self) -> SourceKind:
        """Classify the downloaded database as ``legacy``, ``migrated`` or ``unrecognized``.

        Replaces the old ``PRAGMA user_version`` guard: Alembic's ``alembic_version``
        table is the marker now. Introspection is read-only, so its transaction is
        rolled back before ``migrate`` opens the setup transaction.
        """
        conn = self._connection
        insp = inspect(conn)
        tables = set(insp.get_table_names())
        source: SourceKind = "unrecognized"
        if {"Product", "SellerProduct"} <= tables:
            product_cols = {c["name"]: c for c in insp.get_columns("Product")}
            sp_cols = {c["name"] for c in insp.get_columns("SellerProduct")}
            product_id_type = str(product_cols.get("Id", {}).get("type", "")).upper()

            if (
                {"Brand", "Category"} <= product_cols.keys()
                and "BrandId" not in product_cols
                and product_id_type.startswith("INTEGER")
                and {"SellerName", "SellerProductId"} <= sp_cols
                and "ExternalSku" not in sp_cols
                and not {"Brand", "Category", "Seller", "alembic_version"} & tables
            ):
                source = "legacy"
            elif (
                {"Brand", "Category", "ProductCategory", "Seller", "alembic_version"} <= tables
                and "BrandId" in product_cols
                and "CategoryId" not in product_cols
                and "TEXT" in product_id_type
                and "ExternalSku" in sp_cols
            ):
                source = "migrated"

        if conn.in_transaction():
            conn.rollback()
        logger.info("source classified as=%s", source)
        return source

    def migrate(self) -> None:
        """Run ``alembic upgrade head`` inside one transaction (a no-op if already at head)."""
        conn = self._connection
        trans = conn.begin()
        try:
            command.upgrade(self._alembic_config(conn), "head")
            trans.commit()
        except Exception:
            if trans.is_active:
                trans.rollback()
            logger.error("schema refactor failed; previous output preserved")
            raise
        logger.info("schema refactor committed")

    def enable_foreign_keys(self) -> None:
        """Enable and verify FK enforcement on the import connection (SQLite: outside a txn)."""
        conn = self._connection
        conn.exec_driver_sql("PRAGMA foreign_keys = ON")
        if conn.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
            raise RuntimeError("foreign key enforcement could not be enabled")
        conn.commit()
        logger.info("foreign key enforcement enabled")

    @staticmethod
    def _alembic_config(connection: Connection) -> AlembicConfig:
        cfg = AlembicConfig()
        cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
        cfg.set_main_option("sqlalchemy.url", "sqlite://")  # placeholder; connection is injected
        cfg.attributes["connection"] = connection
        return cfg

    # ----------------------------------------------------------------- #
    # Feed import
    # ----------------------------------------------------------------- #
    def prepare_import(self, *, matcher: str, threshold: float) -> None:
        self._similarity = build_similarity(matcher)
        self._threshold = threshold
        self._importer = self._build_importer()

    def _build_importer(self) -> FeedImporter:
        """(Re)build the in-memory indexes and release the read transaction they opened."""
        conn = self._connection
        importer = FeedImporter(conn, load_catalog(conn), self._similarity, self._threshold)
        conn.commit()
        return importer

    @contextmanager
    def item_transaction(self) -> Iterator[FeedImporter]:
        """One feed entry: commit on success; on failure roll back and refresh the indexes."""
        if self._importer is None:
            raise RuntimeError("prepare_import() must run before item_transaction()")
        conn = self._connection
        trans = conn.begin()
        try:
            yield self._importer
        except Exception:
            if trans.is_active:
                trans.rollback()
            self._importer = self._build_importer()
            raise
        trans.commit()
