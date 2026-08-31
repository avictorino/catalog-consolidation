from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine

from consolidation import db_upgrade
from consolidation.feed import ProductEntry, Report
from consolidation.importer import (
    CatalogIndex,
    CatalogProduct,
    FeedImporter,
    load_catalog,
    resolve_product,
)
from consolidation.similarity import DifflibSimilarity, RapidFuzzSimilarity


def test_translation_resolves_with_both_matchers() -> None:
    catalog = CatalogIndex(
        [CatalogProduct("product-id", "Router WiFi 6 TP-Link", "TP-Link", "Networking")]
    )
    entry = ProductEntry.model_validate(
        {
            "Id": "sku",
            "SellerName": "seller",
            "Name": "Roteador WiFi 6 TP-Link",
            "Brand": "TP-Link",
            "Category": "Networking",
        }
    )
    for similarity in (DifflibSimilarity(), RapidFuzzSimilarity()):
        product, reason, score = resolve_product(catalog, entry, similarity, 0.90)
        assert product is not None
        assert reason is None
        assert score == pytest.approx(0.909, abs=0.001)


def test_threshold_rejects_boundary_match() -> None:
    catalog = CatalogIndex(
        [CatalogProduct("product-id", "Router WiFi 6 TP-Link", "TP-Link", "Networking")]
    )
    entry = ProductEntry.model_validate(
        {
            "Id": "sku",
            "SellerName": "seller",
            "Name": "Roteador WiFi 6 TP-Link",
            "Brand": "TP-Link",
            "Category": "Networking",
        }
    )
    product, reason, score = resolve_product(catalog, entry, DifflibSimilarity(), 0.91)
    assert product is None
    assert reason is None
    assert score is None


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
                db_upgrade.SellerProduct.select().with_only_columns(
                    db_upgrade.SellerProduct.c.ExternalSku
                )
            ).all() == [("sku-1",)]
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
