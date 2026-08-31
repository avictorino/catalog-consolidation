"""Feed identity resolution and idempotent SQLAlchemy Core persistence."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import insert, select
from sqlalchemy.engine import Connection

from consolidation import db_upgrade
from consolidation.feed import ProductEntry, Report, screen_entry
from consolidation.similarity import Similarity
from consolidation.util import normalize

logger = logging.getLogger("consolidation")


@dataclass
class CatalogProduct:
    id: str
    name: str
    brand: str | None
    category: str | None

    @property
    def normalized_name(self) -> str:
        return normalize(self.name)


class CatalogIndex:
    """In-memory view of the small catalog used by exact and fuzzy lookup."""

    def __init__(self, products: list[CatalogProduct]) -> None:
        self.products = products
        self.by_name: dict[str, list[CatalogProduct]] = {}
        for product in products:
            self.by_name.setdefault(product.normalized_name, []).append(product)

    def add(self, product: CatalogProduct) -> None:
        self.products.append(product)
        self.by_name.setdefault(product.normalized_name, []).append(product)


def load_catalog(conn: Connection) -> CatalogIndex:
    """Read the target catalog once; fuzzy retrieval remains a plain product scan."""
    query = select(
        db_upgrade.Product.c.Id,
        db_upgrade.Product.c.Name,
        db_upgrade.Brand.c.Name.label("BrandName"),
        db_upgrade.Category.c.Name.label("CategoryName"),
    ).select_from(
        db_upgrade.Product.outerjoin(
            db_upgrade.Brand, db_upgrade.Product.c.BrandId == db_upgrade.Brand.c.Id
        ).outerjoin(
            db_upgrade.Category,
            db_upgrade.Product.c.CategoryId == db_upgrade.Category.c.Id,
        )
    )
    products = [
        CatalogProduct(id, name, brand, category)
        for id, name, brand, category in conn.execute(query)
    ]
    logger.info("catalog loaded products=%d", len(products))
    return CatalogIndex(products)


def _digit_tokens(value: str) -> Counter[str]:
    return Counter(token for token in value.split() if any(char.isdigit() for char in token))


def _brand_compatible(feed_brand: str | None, product_brand: str | None) -> bool:
    feed_norm = normalize(feed_brand)
    product_norm = normalize(product_brand)
    return not feed_norm or not product_norm or feed_norm == product_norm


def _fuzzy_eligible(
    entry: ProductEntry,
    product: CatalogProduct,
    similarity: Similarity,
    threshold: float,
) -> tuple[bool, float]:
    entry_name = normalize(entry.Name)
    product_name = product.normalized_name
    if not _brand_compatible(entry.Brand, product.brand):
        return False, 0.0
    if len(entry_name.split()) != len(product_name.split()):
        return False, 0.0
    if _digit_tokens(entry_name) != _digit_tokens(product_name):
        return False, 0.0
    score = similarity.score(entry_name, product_name)
    return score >= threshold, score


def resolve_product(
    catalog: CatalogIndex,
    entry: ProductEntry,
    similarity: Similarity,
    threshold: float,
) -> tuple[CatalogProduct | None, str | None, float | None]:
    """Resolve an entry to one product, or return a skip reason / new-product signal."""
    normalized_name = normalize(entry.Name)
    exact_matches = catalog.by_name.get(normalized_name, [])
    if exact_matches:
        if len(exact_matches) != 1:
            return None, "ambiguous exact name", None
        product = exact_matches[0]
        if not _brand_compatible(entry.Brand, product.brand):
            return None, "brand conflict", None
        return product, None, None

    candidates: list[tuple[CatalogProduct, float]] = []
    for product in catalog.products:
        eligible, score = _fuzzy_eligible(entry, product, similarity, threshold)
        if eligible:
            candidates.append((product, score))

    if len(candidates) > 1:
        return None, "ambiguous fuzzy candidates", None
    if candidates:
        product, score = candidates[0]
        return product, None, score
    return None, None, None


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
        self.brand_ids = self._load_reference_ids(db_upgrade.Brand)
        self.category_ids = self._load_reference_ids(db_upgrade.Category)
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
            for id, name in self.conn.execute(
                select(db_upgrade.Seller.c.Id, db_upgrade.Seller.c.Name)
            )
        }

    def _reference_id(self, table, cache: dict[str, str], raw_name: str | None) -> str | None:
        normalized = normalize(raw_name)
        if not normalized:
            return None
        existing = cache.get(normalized)
        if existing:
            return existing
        new_id = db_upgrade.new_uuid()
        self.conn.execute(insert(table).values(Id=new_id, Name=normalized.title()))
        cache[normalized] = new_id
        return new_id

    def _seller_id(self, seller_name: str) -> str:
        existing = self.seller_ids.get(seller_name)
        if existing:
            return existing
        new_id = db_upgrade.new_uuid()
        self.conn.execute(insert(db_upgrade.Seller).values(Id=new_id, Name=seller_name))
        self.seller_ids[seller_name] = new_id
        return new_id

    def _insert_product(self, entry: ProductEntry) -> CatalogProduct:
        brand_id = self._reference_id(db_upgrade.Brand, self.brand_ids, entry.Brand)
        category_id = self._reference_id(db_upgrade.Category, self.category_ids, entry.Category)
        product = CatalogProduct(db_upgrade.new_uuid(), entry.Name, entry.Brand, entry.Category)
        self.conn.execute(
            insert(db_upgrade.Product).values(
                Id=product.id,
                Name=product.name,
                BrandId=brand_id,
                CategoryId=category_id,
            )
        )
        self.catalog.add(product)
        return product

    def _existing_sku_product(self, seller_id: str, external_sku: str) -> str | None:
        return self.conn.execute(
            select(db_upgrade.SellerProduct.c.ProductId).where(
                db_upgrade.SellerProduct.c.SellerId == seller_id,
                db_upgrade.SellerProduct.c.ExternalSku == external_sku,
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
            select(db_upgrade.SellerProduct.c.ExternalSku).where(
                db_upgrade.SellerProduct.c.SellerId == seller_id,
                db_upgrade.SellerProduct.c.ProductId == product.id,
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

        statement = insert(db_upgrade.SellerProduct).values(
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

        category_diverges = (
            product.category
            and entry.Category
            and normalize(product.category) != normalize(entry.Category)
        )
        if category_diverges:
            logger.warning(
                "event=category_divergence record=%d product_id=%s",
                record_index,
                product.id,
            )

        seller_id = seller_id or self._seller_id(entry.SellerName)
        self._link(seller_id, product, entry, record_index, report)
