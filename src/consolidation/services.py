"""Domain services & ports.

Logic that belongs to the domain but does not sit naturally on a single entity —
here, deciding which catalog product (if any) a seller submission refers to. Also
declares the ``Similarity`` port; concrete fuzzy backends live in
``infrastructure``.

Depends on: :mod:`consolidation.domain`.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Protocol

from consolidation.domain import Catalog, Product, Submission, brands_compatible, normalize


class Similarity(Protocol):
    """Port: an interchangeable string-similarity backend (difflib / rapidfuzz)."""

    name: str
    suggested_threshold: float

    def score(self, a: str, b: str) -> float:
        """Return a score in the inclusive range [0, 1] for two normalized strings."""


def _digit_tokens(value: str) -> Counter[str]:
    return Counter(token for token in value.split() if any(char.isdigit() for char in token))


def _fuzzy_eligible(
    submission: Submission,
    product: Product,
    similarity: Similarity,
    threshold: float,
) -> tuple[bool, float]:
    entry_name = normalize(submission.Name)
    product_name = product.normalized_name
    if not product.brand_compatible_with(submission.Brand):
        return False, 0.0
    if len(entry_name.split()) != len(product_name.split()):
        return False, 0.0
    if _digit_tokens(entry_name) != _digit_tokens(product_name):
        return False, 0.0
    score = similarity.score(entry_name, product_name)
    return score >= threshold, score


def resolve_product(
    catalog: Catalog,
    submission: Submission,
    similarity: Similarity,
    threshold: float,
) -> tuple[Product | None, str | None, float | None]:
    """Resolve a submission to exactly one product.

    Returns ``(product, reason, score)``:

    * ``(product, None, score?)`` — matched (``score`` set only for a fuzzy match);
    * ``(None, reason, None)``    — skip and report (ambiguous / brand conflict);
    * ``(None, None, None)``      — genuinely new product.

    Order of rules: exact normalized name -> identical word multiset (any order)
    -> gated fuzzy scan. Category never participates; the feed ``Id`` never does.
    """
    normalized_name = normalize(submission.Name)

    exact_matches = catalog.find_by_name(normalized_name)
    if exact_matches:
        if len(exact_matches) != 1:
            return None, "ambiguous exact name", None
        product = exact_matches[0]
        if not product.brand_compatible_with(submission.Brand):
            return None, "brand conflict", None
        return product, None, None

    name_tokens = Counter(normalized_name.split())
    word_matches = [
        product
        for product in catalog.products
        if name_tokens and product.name_tokens() == name_tokens
    ]
    if word_matches:
        compatible = [p for p in word_matches if p.brand_compatible_with(submission.Brand)]
        if not compatible:
            return None, "brand conflict", None
        if len(compatible) > 1:
            return None, "ambiguous word order", None
        return compatible[0], None, None

    candidates: list[tuple[Product, float]] = []
    for product in catalog.products:
        eligible, score = _fuzzy_eligible(submission, product, similarity, threshold)
        if eligible:
            candidates.append((product, score))

    if len(candidates) > 1:
        return None, "ambiguous fuzzy candidates", None
    if candidates:
        product, score = candidates[0]
        return product, None, score
    return None, None, None


@dataclass
class Resolution:
    """Object face of :func:`resolve_product` for readers who prefer it."""

    product: Product | None
    reason: str | None
    score: float | None

    @property
    def is_match(self) -> bool:
        return self.product is not None

    @property
    def is_skip(self) -> bool:
        return self.reason is not None

    @property
    def is_new_product(self) -> bool:
        return self.product is None and self.reason is None


class ProductIdentityResolver:
    """Domain service: given the catalog, decide what a submission refers to."""

    def __init__(self, similarity: Similarity, threshold: float) -> None:
        self.similarity = similarity
        self.threshold = threshold

    def resolve(self, catalog: Catalog, submission: Submission) -> Resolution:
        product, reason, score = resolve_product(
            catalog, submission, self.similarity, self.threshold
        )
        return Resolution(product, reason, score)


# Kept for symmetry with the domain helper name used across the codebase.
brand_compatible = brands_compatible
