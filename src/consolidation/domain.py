"""Domain layer — the ubiquitous language of catalog consolidation.

Pure business model: entities, value objects and the identity rules that make a
seller listing "the same product" as a catalog entry. This module imports only
the standard library — no SQLAlchemy, no pydantic, no HTTP. That isolation is the
point: every rule here can be read and tested without a database.

Depends on: nothing (project-internal).
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol

# --------------------------------------------------------------------------- #
# Value objects / domain primitives
# --------------------------------------------------------------------------- #
# Quote / apostrophe / prime marks are removed with no replacement, so
# ``Levi's`` -> ``levis`` (matches catalog ``Levis``) and ``12.9''`` -> ``12 9``.
_QUOTE_MARKS = "'`\"‘’“”′″´ʼ"  # noqa: RUF001 -- enumerating quote/apostrophe glyphs on purpose
_QUOTE_STRIP = {ord(ch): None for ch in _QUOTE_MARKS}
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize(value: str | None) -> str:
    """Return the normalized form of ``value`` (``""`` for ``None``/blank).

    1. Unicode NFKD, drop combining marks (accent folding).
    2. Lowercase.
    3. Strip quote and apostrophe marks (no replacement).
    4. Replace every run of remaining non-alphanumeric characters with a single space.
    5. Collapse whitespace, trim.

    This single function is applied identically to catalog names, feed names,
    brand values and category values. SQLite ``lower()`` is ASCII-only and cannot
    fold accents (``Câmera`` -> ``camera``), so identity normalization lives here.
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    unquoted = without_marks.lower().translate(_QUOTE_STRIP)
    spaced = _NON_ALNUM.sub(" ", unquoted)
    return " ".join(spaced.split())


def new_uuid() -> str:
    """A fresh ``uuid4`` as a 36-char string, minted in Python (no DB coordination)."""
    return str(uuid.uuid4())


def brands_compatible(one: str | None, other: str | None) -> bool:
    """Two brands are compatible when they normalize equal, or either is absent.

    Brand is only a tie-break gate for product identity, never identity itself.
    """
    a, b = normalize(one), normalize(other)
    return not a or not b or a == b


class Submission(Protocol):
    """The shape the anti-corruption layer must hand the domain for one listing.

    ``infrastructure.ProductEntry`` (a pydantic model) structurally satisfies this;
    the domain never depends on that concrete type.
    """

    Id: str
    SellerName: str
    Name: str
    Brand: str | None
    Category: str | None


# --------------------------------------------------------------------------- #
# Entities
# --------------------------------------------------------------------------- #
@dataclass
class Product:
    """A catalog product. Aggregate root for its category memberships.

    Business identity is the normalized ``name`` (see :func:`normalize`); ``id`` is
    just the storage key. ``categories`` holds the capitalized normalized display
    names of the categories this product belongs to.
    """

    id: str
    name: str
    brand: str | None
    categories: tuple[str, ...] = ()

    @property
    def normalized_name(self) -> str:
        return normalize(self.name)

    def name_tokens(self) -> Counter[str]:
        return Counter(self.normalized_name.split())

    def brand_compatible_with(self, brand: str | None) -> bool:
        return brands_compatible(self.brand, brand)

    def has_category(self, normalized: str) -> bool:
        return any(normalize(existing) == normalized for existing in self.categories)

    def record_category(self, raw_category: str | None) -> bool:
        """Add a category membership in memory. Returns ``True`` when the incoming
        category *diverges* — the product already had categories and none matched.

        Persisting the membership row is the repository's job; this only keeps the
        in-memory aggregate consistent and reports the divergence for logging.
        """
        normalized = normalize(raw_category)
        if not normalized:
            return False
        diverges = bool(self.categories) and not self.has_category(normalized)
        if not self.has_category(normalized):
            self.categories = (*self.categories, normalized.title())
        return diverges


@dataclass
class Catalog:
    """In-memory view of the (small) catalog used by identity resolution.

    Not the database — a working set the use case reads once and keeps updated as
    new products are created during a run.
    """

    products: list[Product]
    by_name: dict[str, list[Product]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.by_name:
            for product in self.products:
                self.by_name.setdefault(product.normalized_name, []).append(product)

    def find_by_name(self, normalized_name: str) -> list[Product]:
        return self.by_name.get(normalized_name, [])

    def add(self, product: Product) -> None:
        self.products.append(product)
        self.by_name.setdefault(product.normalized_name, []).append(product)


@dataclass
class ThreatFinding:
    """A feed field that tripped the SQL-injection screen."""

    field: str
    fingerprint: str
    value: str
