from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select

from consolidation import db_upgrade
from consolidation.feed import ProductEntry, Report
from consolidation.importer import (
    CatalogIndex,
    CatalogProduct,
    FeedImporter,
    load_catalog,
    resolve_product,
)
from consolidation.similarity import DifflibSimilarity, RapidFuzzSimilarity, Similarity

from .conftest import PRODUCT_ROWS


@pytest.fixture(params=(DifflibSimilarity, RapidFuzzSimilarity), ids=("difflib", "rapidfuzz"))
def similarity(request: pytest.FixtureRequest) -> Similarity:
    return request.param()


@pytest.mark.parametrize(
    ("catalog_name", "feed_name"),
    [
        ("Smartphone Galaxy S23", "Galaxy S23 Smartphone"),
        ("Câmera Canon EOS R6", "EOS R6 Camera Canon"),
        ("Gaming Gaming Mouse Pro", "Pro Mouse Gaming Gaming"),
    ],
)
def test_reordered_words_resolve_without_lowering_threshold(
    similarity: Similarity, catalog_name: str, feed_name: str
) -> None:
    expected = CatalogProduct("existing", catalog_name, "Brand", None)
    entry = ProductEntry(Id="sku", SellerName="seller", Name=feed_name, Brand="BRAND")

    product, reason, score = resolve_product(CatalogIndex([expected]), entry, similarity, 1.0)

    assert product == expected
    assert reason is None
    assert score is None


@pytest.mark.parametrize(
    ("catalog_name", "feed_name"),
    [
        ("Smartphone Galaxy S23", "Galaxy S24 Smartphone"),
        ("iPhone 15 128GB", "256GB iPhone 15"),
        ("Smartphone Galaxy S23", "Galaxy S23 Ultra Smartphone"),
        ("Camera Canon EOS R6", "EOS R6 Camera Canon Kit"),
        ("Gaming Gaming Mouse Pro", "Pro Mouse Gaming"),
        ("Mouse Pro Pro", "Mouse Mouse Pro"),
    ],
)
def test_word_order_does_not_ignore_product_attributes(
    similarity: Similarity, catalog_name: str, feed_name: str
) -> None:
    catalog = CatalogIndex([CatalogProduct("existing", catalog_name, "Brand", None)])
    entry = ProductEntry(Id="sku", SellerName="seller", Name=feed_name, Brand="Brand")

    product, reason, score = resolve_product(catalog, entry, similarity, 0.90)

    assert product is None
    assert reason is None
    assert score is None


@pytest.mark.parametrize(
    ("first_brand", "second_brand", "expected_id", "expected_reason"),
    [
        ("Samsung", "Other", "first", None),
        ("Samsung", "Samsung", None, "ambiguous word order"),
        ("Samsung", None, None, "ambiguous word order"),
        ("Other", "Other", None, "brand conflict"),
    ],
)
def test_word_order_requires_one_brand_compatible_candidate(
    similarity: Similarity,
    first_brand: str,
    second_brand: str | None,
    expected_id: str | None,
    expected_reason: str | None,
) -> None:
    catalog = CatalogIndex(
        [
            CatalogProduct("first", "Smartphone Galaxy S23", first_brand, None),
            CatalogProduct("second", "Smartphone S23 Galaxy", second_brand, None),
        ]
    )
    entry = ProductEntry(
        Id="sku", SellerName="seller", Name="Galaxy S23 Smartphone", Brand="Samsung"
    )

    product, reason, _ = resolve_product(catalog, entry, similarity, 0.90)

    assert (product.id if product else None) == expected_id
    assert reason == expected_reason


def test_word_order_match_takes_priority_over_fuzzy_candidate(similarity: Similarity) -> None:
    expected = CatalogProduct("existing", "Smartphone Galaxy S23", "Samsung", None)
    catalog = CatalogIndex(
        [expected, CatalogProduct("similar", "Galaxy S23 Smartphones", "Samsung", None)]
    )
    entry = ProductEntry(
        Id="sku", SellerName="seller", Name="Galaxy S23 Smartphone", Brand="Samsung"
    )

    product, reason, score = resolve_product(catalog, entry, similarity, 0.90)

    assert product == expected
    assert reason is None
    assert score is None


def test_exact_name_still_takes_priority_over_word_order(similarity: Similarity) -> None:
    expected = CatalogProduct("exact", "Galaxy S23 Smartphone", "Samsung", None)
    catalog = CatalogIndex(
        [expected, CatalogProduct("reordered", "Smartphone Galaxy S23", "Samsung", None)]
    )
    entry = ProductEntry(
        Id="sku", SellerName="seller", Name="Galaxy S23 Smartphone", Brand="Samsung"
    )

    assert resolve_product(catalog, entry, similarity, 0.90)[0] == expected


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
            assert (
                conn.scalar(select(func.count()).select_from(db_upgrade.Product))
                == PRODUCT_ROWS + 1
            )
            product_id = conn.scalar(
                select(db_upgrade.Product.c.Id).where(
                    db_upgrade.Product.c.Name == "Smartphone Galaxy S23"
                )
            )
            assert product_id is not None
            assert conn.execute(
                select(
                    db_upgrade.SellerProduct.c.ProductId, db_upgrade.SellerProduct.c.ExternalSku
                ).order_by(db_upgrade.SellerProduct.c.ExternalSku)
            ).all() == [(product_id, "sku-1"), (product_id, "sku-2")]
    finally:
        engine.dispose()


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
