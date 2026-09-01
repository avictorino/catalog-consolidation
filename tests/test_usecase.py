from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select

from consolidation import schema
from consolidation.domain import Catalog, Product
from consolidation.infrastructure import DifflibSimilarity, ProductEntry, RapidFuzzSimilarity
from consolidation.repository import CatalogRepositories
from consolidation.services import ProductIdentityResolver, Similarity, resolve_product
from consolidation.usecase import ConsolidateFeedUseCase

from .conftest import PRODUCT_ROWS


@pytest.fixture(params=(DifflibSimilarity, RapidFuzzSimilarity), ids=("difflib", "rapidfuzz"))
def similarity_cls(request: pytest.FixtureRequest) -> type[Similarity]:
    return request.param


@pytest.fixture
def similarity(similarity_cls: type[Similarity]) -> Similarity:
    return similarity_cls(0.90)


@pytest.mark.parametrize(
    ("catalog_name", "feed_name"),
    [
        ("Smartphone Galaxy S23", "Galaxy S23 Smartphone"),
        ("Câmera Canon EOS R6", "EOS R6 Camera Canon"),
        ("Gaming Gaming Mouse Pro", "Pro Mouse Gaming Gaming"),
    ],
)
def test_reordered_words_resolve_without_lowering_threshold(
    similarity_cls: type[Similarity], catalog_name: str, feed_name: str
) -> None:
    expected = Product("existing", catalog_name, "Brand")
    entry = ProductEntry(Id="sku", SellerName="seller", Name=feed_name, Brand="BRAND")

    product, reason, score = resolve_product(Catalog([expected]), entry, similarity_cls(1.0))

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
    catalog = Catalog([Product("existing", catalog_name, "Brand")])
    entry = ProductEntry(Id="sku", SellerName="seller", Name=feed_name, Brand="Brand")

    product, reason, score = resolve_product(catalog, entry, similarity)

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
    catalog = Catalog(
        [
            Product("first", "Smartphone Galaxy S23", first_brand),
            Product("second", "Smartphone S23 Galaxy", second_brand),
        ]
    )
    entry = ProductEntry(
        Id="sku", SellerName="seller", Name="Galaxy S23 Smartphone", Brand="Samsung"
    )

    product, reason, _ = resolve_product(catalog, entry, similarity)

    assert (product.id if product else None) == expected_id
    assert reason == expected_reason


def test_word_order_match_takes_priority_over_fuzzy_candidate(similarity: Similarity) -> None:
    expected = Product("existing", "Smartphone Galaxy S23", "Samsung")
    catalog = Catalog([expected, Product("similar", "Galaxy S23 Smartphones", "Samsung")])
    entry = ProductEntry(
        Id="sku", SellerName="seller", Name="Galaxy S23 Smartphone", Brand="Samsung"
    )

    product, reason, score = resolve_product(catalog, entry, similarity)

    assert product == expected
    assert reason is None
    assert score is None


def test_exact_name_still_takes_priority_over_word_order(similarity: Similarity) -> None:
    expected = Product("exact", "Galaxy S23 Smartphone", "Samsung")
    catalog = Catalog([expected, Product("reordered", "Smartphone Galaxy S23", "Samsung")])
    entry = ProductEntry(
        Id="sku", SellerName="seller", Name="Galaxy S23 Smartphone", Brand="Samsung"
    )

    assert resolve_product(catalog, entry, similarity)[0] == expected


def test_new_product_is_reused_for_reordered_listing(
    migrated_db: Path, similarity_cls: type[Similarity]
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
            conn.exec_driver_sql("PRAGMA foreign_keys = ON")
            # third entry re-processes the reordered listing to prove idempotency
            report = ConsolidateFeedUseCase(
                CatalogRepositories(conn), ProductIdentityResolver(similarity_cls(1.0))
            ).execute(iter([*entries, entries[1]]))

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


def test_translation_resolves_with_both_matchers() -> None:
    catalog = Catalog([Product("product-id", "Router WiFi 6 TP-Link", "TP-Link", ("Networking",))])
    entry = ProductEntry.model_validate(
        {
            "Id": "sku",
            "SellerName": "seller",
            "Name": "Roteador WiFi 6 TP-Link",
            "Brand": "TP-Link",
            "Category": "Networking",
        }
    )
    for similarity in (DifflibSimilarity(0.90), RapidFuzzSimilarity(0.90)):
        product, reason, score = resolve_product(catalog, entry, similarity)
        assert product is not None
        assert reason is None
        assert score == pytest.approx(0.909, abs=0.001)


def test_threshold_rejects_boundary_match() -> None:
    catalog = Catalog([Product("product-id", "Router WiFi 6 TP-Link", "TP-Link", ("Networking",))])
    entry = ProductEntry.model_validate(
        {
            "Id": "sku",
            "SellerName": "seller",
            "Name": "Roteador WiFi 6 TP-Link",
            "Brand": "TP-Link",
            "Category": "Networking",
        }
    )
    product, reason, score = resolve_product(catalog, entry, DifflibSimilarity(0.91))
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
            conn.exec_driver_sql("PRAGMA foreign_keys = ON")
            report = ConsolidateFeedUseCase(
                CatalogRepositories(conn), ProductIdentityResolver(DifflibSimilarity(0.90))
            ).execute(iter([entry, entry.model_copy(update={"Id": "sku-2"})]))

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
            conn.exec_driver_sql("PRAGMA foreign_keys = ON")
            report = ConsolidateFeedUseCase(
                CatalogRepositories(conn), ProductIdentityResolver(DifflibSimilarity(0.90))
            ).execute(
                iter(
                    [
                        ProductEntry.model_validate(
                            {
                                "Id": "same-sku",
                                "SellerName": "seller",
                                "Name": "Camera Canon EOS R6",
                                "Brand": "Canon",
                                "Category": "Photography",
                            }
                        ),
                        ProductEntry.model_validate(
                            {
                                "Id": "same-sku",
                                "SellerName": "seller",
                                "Name": "Cordless Drill",
                                "Brand": "BLACK+DECKER",
                                "Category": "Tools",
                            }
                        ),
                    ]
                )
            )
            assert report.linked == 1
            assert report.skipped == 1
            assert report.skipped_entries == [
                {"record_index": 1, "reason": "external SKU conflict"}
            ]
    finally:
        engine.dispose()
