from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, func, select

from consolidation import schema
from consolidation.entries import ProductEntry
from consolidation.report import Report
from consolidation.similarity import DifflibSimilarity, Similarity
from consolidation.sqlite_store import FeedImporter, load_catalog

from .conftest import PRODUCT_ROWS


def test_new_product_is_reused_for_reordered_listing(
    migrated_db: Path, similarity: Similarity
) -> None:
    entries = [
        ProductEntry(
            Id="sku-1", SellerName="First seller", Name="Smartphone Galaxy S23", Brand="Samsung"
        ),
        ProductEntry(
            Id="sku-2", SellerName="Second seller", Name="Galaxy S23 Smartphone", Brand="Samsung"
        ),
    ]
    engine = create_engine(f"sqlite:///{migrated_db}")
    try:
        with engine.connect() as conn:
            importer = FeedImporter(conn, load_catalog(conn), similarity, 1.0)
            conn.commit()
            report = Report()
            for index, entry in enumerate(entries):
                with conn.begin():
                    importer.process(entry, index, report)
            with conn.begin():
                importer.process(entries[1], 2, report)

            assert report.new == 1
            assert report.linked == 2
            assert report.skipped == 0
            assert conn.scalar(select(func.count()).select_from(schema.Product)) == PRODUCT_ROWS + 1
            product_id = conn.scalar(
                select(schema.Product.c.Id).where(schema.Product.c.Name == "Smartphone Galaxy S23")
            )
            assert product_id is not None
            assert conn.execute(
                select(
                    schema.SellerProduct.c.ProductId, schema.SellerProduct.c.ExternalSku
                ).order_by(schema.SellerProduct.c.ExternalSku)
            ).all() == [(product_id, "sku-1"), (product_id, "sku-2")]
    finally:
        engine.dispose()


def test_importer_persists_links_idempotently(migrated_db: Path, caplog) -> None:
    engine = create_engine(f"sqlite:///{migrated_db}")
    entry = ProductEntry.model_validate(
        {
            "Id": "sku-1",
            "SellerName": "GardenStore",
            "Name": "Camera Canon EOS R6",
            "Brand": "Canon",
            "Category": "Photo",
        }
    )
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            catalog = load_catalog(conn)
            importer = FeedImporter(conn, catalog, DifflibSimilarity(), 0.90)
            report = Report()
            importer.process(entry, 0, report)
            importer.process(entry.model_copy(update={"Id": "sku-2"}), 1, report)
            trans.commit()

            assert report.new == 0
            assert report.linked == 1
            assert report.skipped == 0
            assert report.threat == 0
            assert any("category_divergence" in record.message for record in caplog.records)
            assert conn.execute(
                schema.SellerProduct.select().with_only_columns(schema.SellerProduct.c.ExternalSku)
            ).all() == [("sku-1",)]
            product_id = conn.scalar(
                select(schema.Product.c.Id).where(schema.Product.c.Name == "Camera Canon EOS R6")
            )
            category_names = conn.execute(
                select(schema.Category.c.Name)
                .select_from(
                    schema.ProductCategory.join(
                        schema.Category,
                        schema.ProductCategory.c.CategoryId == schema.Category.c.Id,
                    )
                )
                .where(schema.ProductCategory.c.ProductId == product_id)
                .order_by(schema.Category.c.Name)
            ).all()
            assert category_names == [("Photo",), ("Photography",)]
    finally:
        engine.dispose()


def test_same_sku_cannot_be_reassociated(migrated_db: Path) -> None:
    engine = create_engine(f"sqlite:///{migrated_db}")
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            catalog = load_catalog(conn)
            importer = FeedImporter(conn, catalog, DifflibSimilarity(), 0.90)
            report = Report()
            importer.process(
                ProductEntry.model_validate(
                    {
                        "Id": "same-sku",
                        "SellerName": "seller",
                        "Name": "Camera Canon EOS R6",
                        "Brand": "Canon",
                        "Category": "Photography",
                    }
                ),
                0,
                report,
            )
            importer.process(
                ProductEntry.model_validate(
                    {
                        "Id": "same-sku",
                        "SellerName": "seller",
                        "Name": "Cordless Drill",
                        "Brand": "BLACK+DECKER",
                        "Category": "Tools",
                    }
                ),
                1,
                report,
            )
            trans.commit()
            assert report.linked == 1
            assert report.skipped == 1
            assert report.skipped_entries == [
                {"record_index": 1, "reason": "external SKU conflict"}
            ]
    finally:
        engine.dispose()
