"""Identity resolution: match one validated feed entry to at most one catalog product.

Pure in-memory logic — no I/O. The exact / word-order / fuzzy stages are specified in
``spec/contract.md#matching-stages``.
"""

from __future__ import annotations

from collections import Counter

from consolidation.catalog import CatalogIndex, CatalogProduct
from consolidation.entries import ProductEntry
from consolidation.normalize import normalize
from consolidation.similarity import Similarity


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

    name_tokens = Counter(normalized_name.split())
    word_matches = [
        product
        for product in catalog.products
        if name_tokens and Counter(product.normalized_name.split()) == name_tokens
    ]
    if word_matches:
        compatible = [
            product for product in word_matches if _brand_compatible(entry.Brand, product.brand)
        ]
        if not compatible:
            return None, "brand conflict", None
        if len(compatible) > 1:
            return None, "ambiguous word order", None
        return compatible[0], None, None

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
