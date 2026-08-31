"""In-memory catalog model used by matching and by the feed writer."""

from __future__ import annotations

from dataclasses import dataclass

from consolidation.normalize import normalize


@dataclass
class CatalogProduct:
    id: str
    name: str
    brand: str | None
    categories: tuple[str, ...] = ()

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
