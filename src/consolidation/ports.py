"""The boundaries the ``consolidate`` use case depends on.

``consolidate.run`` is written against these protocols; ``consolidation.sqlite_store``
provides the concrete SQLite/Alembic implementation, and tests can substitute a fake.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Literal, Protocol

from consolidation.entries import ProductEntry
from consolidation.report import Report

SourceKind = Literal["legacy", "migrated", "unrecognized"]


class EntryWriter(Protocol):
    """Persists one validated feed entry inside an open transaction."""

    def process(self, entry: ProductEntry, record_index: int, report: Report) -> None: ...


class CatalogStore(Protocol):
    """The downloaded catalog: schema lifecycle plus per-entry import transactions."""

    def __enter__(self) -> CatalogStore: ...

    def __exit__(self, *exc: object) -> None: ...

    def classify_source(self) -> SourceKind: ...

    def migrate(self) -> None: ...

    def enable_foreign_keys(self) -> None: ...

    def prepare_import(self, *, matcher: str, threshold: float) -> None: ...

    def item_transaction(self) -> AbstractContextManager[EntryWriter]: ...
