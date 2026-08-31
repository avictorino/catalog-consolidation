"""``CatalogRepository`` — the single object that owns the downloaded database.

Every interaction with the SQLite copy goes through this class: the engine/connection
lifecycle, the Alembic-driven schema refactor, foreign-key enforcement, and the
one-transaction-per-entry feed import. ``consolidation.pipeline`` composes it and never
imports SQLAlchemy or Alembic itself.

``pipeline.run`` depends only on the ``Catalog`` protocol below, so a test can pass a
fake in place of a real SQLite-backed repository.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Protocol

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine

from consolidation import _refactor
from consolidation.importer import FeedImporter, load_catalog
from consolidation.similarity import Similarity, build_similarity

logger = logging.getLogger("consolidation")

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


class Catalog(Protocol):
    """The database surface ``pipeline.run`` depends on (a seam for tests)."""

    def __enter__(self) -> Catalog: ...

    def __exit__(self, *exc: object) -> None: ...

    def classify_source(self) -> _refactor.SourceKind: ...

    def migrate(self) -> None: ...

    def enable_foreign_keys(self) -> None: ...

    def prepare_import(self, *, matcher: str, threshold: float) -> None: ...

    def item_transaction(self) -> AbstractContextManager[FeedImporter]: ...


class CatalogRepository:
    """Own one SQLite connection for the lifetime of a single consolidation run."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._engine: Engine | None = None
        self._conn: Connection | None = None
        self._similarity: Similarity | None = None
        self._threshold = 0.0
        self._importer: FeedImporter | None = None

    # ----------------------------------------------------------------- #
    # Lifecycle
    # ----------------------------------------------------------------- #
    def __enter__(self) -> CatalogRepository:
        self._engine = create_engine(f"sqlite:///{self._db_path}")
        self._conn = self._engine.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            if self._conn is not None:
                if self._conn.in_transaction():
                    self._conn.rollback()
                self._conn.close()
        finally:
            if self._engine is not None:
                self._engine.dispose()
            self._conn = None
            self._engine = None
            self._importer = None

    @property
    def _connection(self) -> Connection:
        if self._conn is None:
            raise RuntimeError("CatalogRepository must be used as a context manager")
        return self._conn

    # ----------------------------------------------------------------- #
    # Schema
    # ----------------------------------------------------------------- #
    def classify_source(self) -> _refactor.SourceKind:
        conn = self._connection
        source = _refactor.classify_source(conn)
        if conn.in_transaction():
            conn.rollback()  # introspection is read-only; don't hold the transaction open
        logger.info("source classified as=%s", source)
        return source

    def migrate(self) -> None:
        """Run ``alembic upgrade head`` inside one transaction (a no-op if already at head)."""
        conn = self._connection
        trans = conn.begin()
        try:
            command.upgrade(self._alembic_config(conn), "head")
            trans.commit()
        except Exception:
            if trans.is_active:
                trans.rollback()
            logger.error("schema refactor failed; previous output preserved")
            raise
        logger.info("schema refactor committed")

    def enable_foreign_keys(self) -> None:
        """Enable and verify FK enforcement on the import connection (SQLite: outside a txn)."""
        conn = self._connection
        conn.exec_driver_sql("PRAGMA foreign_keys = ON")
        if conn.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
            raise RuntimeError("foreign key enforcement could not be enabled")
        conn.commit()
        logger.info("foreign key enforcement enabled")

    @staticmethod
    def _alembic_config(connection: Connection) -> AlembicConfig:
        cfg = AlembicConfig()
        cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
        cfg.set_main_option("sqlalchemy.url", "sqlite://")  # placeholder; connection is injected
        cfg.attributes["connection"] = connection
        return cfg

    # ----------------------------------------------------------------- #
    # Feed import
    # ----------------------------------------------------------------- #
    def prepare_import(self, *, matcher: str, threshold: float) -> None:
        self._similarity = build_similarity(matcher)
        self._threshold = threshold
        self._importer = self._build_importer()

    def _build_importer(self) -> FeedImporter:
        """(Re)build the in-memory indexes and release the read transaction they opened."""
        conn = self._connection
        importer = FeedImporter(conn, load_catalog(conn), self._similarity, self._threshold)
        conn.commit()
        return importer

    @contextmanager
    def item_transaction(self) -> Iterator[FeedImporter]:
        """One feed entry: commit on success; on failure roll back and refresh the indexes."""
        if self._importer is None:
            raise RuntimeError("prepare_import() must run before item_transaction()")
        conn = self._connection
        trans = conn.begin()
        try:
            yield self._importer
        except Exception:
            if trans.is_active:
                trans.rollback()
            self._importer = self._build_importer()
            raise
        trans.commit()
