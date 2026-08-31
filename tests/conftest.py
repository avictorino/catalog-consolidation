"""Shared fixtures: a small legacy ``catalog.db`` and a helper to run the refactor."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine

from consolidation.pipeline import _alembic_config

# A legacy catalog exercising every migration concern at tiny scale:
# - two brands that merge on normalization (BLACK+DECKER / Black+DECKER)
# - a NULL brand and a NULL category
# - a category shared by several products
LEGACY_SQL = """
CREATE TABLE Product (
    Id INTEGER PRIMARY KEY AUTOINCREMENT,
    Name TEXT NOT NULL,
    Brand TEXT,
    Category TEXT
);
CREATE TABLE SellerProduct (
    Id INTEGER PRIMARY KEY AUTOINCREMENT,
    SellerName TEXT NOT NULL,
    ProductId INTEGER NOT NULL CONSTRAINT FK_Product_Id REFERENCES Product (Id),
    SellerProductId INTEGER NOT NULL
);
INSERT INTO Product (Name, Brand, Category) VALUES
    ('Camera Canon EOS R6', 'Canon', 'Photography'),
    ('Cordless Drill', 'BLACK+DECKER', 'Tools'),
    ('Impact Driver', 'Black+DECKER', 'Tools'),
    ('Generic Widget', NULL, 'Tools'),
    ('Mystery Item', NULL, NULL),
    ('Wool Rug 6ft', 'Ikea', NULL);
"""

BRAND_ROWS = 3  # canon, black decker, ikea
CATEGORY_ROWS = 2  # photography, tools
PRODUCT_ROWS = 6


def apply_refactor(db_path: Path) -> None:
    """Run Alembic ``upgrade head`` inside one transaction, like the pipeline does."""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                command.upgrade(_alembic_config(conn), "head")
                trans.commit()
            except Exception:
                trans.rollback()
                raise
    finally:
        engine.dispose()


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    db = tmp_path / "catalog.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(LEGACY_SQL)
        conn.commit()
    finally:
        conn.close()
    return db


@pytest.fixture
def migrated_db(legacy_db: Path) -> Path:
    apply_refactor(legacy_db)
    return legacy_db
