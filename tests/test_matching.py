from __future__ import annotations

import pytest

from consolidation.catalog import CatalogIndex, CatalogProduct
from consolidation.entries import ProductEntry
from consolidation.matching import resolve_product
from consolidation.similarity import DifflibSimilarity, RapidFuzzSimilarity, Similarity


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
    expected = CatalogProduct("existing", catalog_name, "Brand")
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
    catalog = CatalogIndex([CatalogProduct("existing", catalog_name, "Brand")])
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
            CatalogProduct("first", "Smartphone Galaxy S23", first_brand),
            CatalogProduct("second", "Smartphone S23 Galaxy", second_brand),
        ]
    )
    entry = ProductEntry(
        Id="sku", SellerName="seller", Name="Galaxy S23 Smartphone", Brand="Samsung"
    )

    product, reason, _ = resolve_product(catalog, entry, similarity, 0.90)

    assert (product.id if product else None) == expected_id
    assert reason == expected_reason


def test_word_order_match_takes_priority_over_fuzzy_candidate(similarity: Similarity) -> None:
    expected = CatalogProduct("existing", "Smartphone Galaxy S23", "Samsung")
    catalog = CatalogIndex(
        [expected, CatalogProduct("similar", "Galaxy S23 Smartphones", "Samsung")]
    )
    entry = ProductEntry(
        Id="sku", SellerName="seller", Name="Galaxy S23 Smartphone", Brand="Samsung"
    )

    product, reason, score = resolve_product(catalog, entry, similarity, 0.90)

    assert product == expected
    assert reason is None
    assert score is None


def test_exact_name_still_takes_priority_over_word_order(similarity: Similarity) -> None:
    expected = CatalogProduct("exact", "Galaxy S23 Smartphone", "Samsung")
    catalog = CatalogIndex(
        [expected, CatalogProduct("reordered", "Smartphone Galaxy S23", "Samsung")]
    )
    entry = ProductEntry(
        Id="sku", SellerName="seller", Name="Galaxy S23 Smartphone", Brand="Samsung"
    )

    assert resolve_product(catalog, entry, similarity, 0.90)[0] == expected


def test_translation_resolves_with_both_matchers() -> None:
    catalog = CatalogIndex(
        [CatalogProduct("product-id", "Router WiFi 6 TP-Link", "TP-Link", ("Networking",))]
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
        [CatalogProduct("product-id", "Router WiFi 6 TP-Link", "TP-Link", ("Networking",))]
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
