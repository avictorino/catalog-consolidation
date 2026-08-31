"""Refactor the legacy catalog into the normalized model.

Drops the two denormalized tables and rebuilds them alongside three reference tables
(``Brand``, ``Category``, ``Seller``), every primary key a Python-minted ``uuid4``
``TEXT``. Target schema and steps: ``spec/data-profile.md#refactored-database``.

One-way: an already-migrated database carries this revision in ``alembic_version`` and
``upgrade head`` is a no-op.

SQLite quirk: ``PRAGMA foreign_keys`` is a no-op inside a transaction, and the whole
refactor runs in the setup transaction. FK enforcement stays off during the rebuild;
``PRAGMA foreign_key_check`` validates the result. After that transaction commits,
``CatalogRepository`` enables and verifies enforcement before importing the feed.

Revision ID: 0001
Revises:
Create Date: 2026-08-31
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Literal

from alembic import op
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from consolidation.normalize import normalize
from consolidation.schema import new_uuid

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("consolidation")


def upgrade() -> None:
    conn = op.get_bind()

    _create_staging_tables(conn)
    brand_map = _extract_reference(conn, "Brand")
    category_map = _extract_reference(conn, "Category")
    _extract_product_sellers(conn)
    product_id_map = _rebuild_product(conn, brand_map, category_map)
    _rebuild_seller_product(conn, product_id_map)
    _swap_tables(conn)
    _foreign_key_check(conn)


def downgrade() -> None:
    raise NotImplementedError("the catalog refactor is one-way")


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #
_STAGING_DDL = (
    """
    CREATE TABLE Brand (
        Id   TEXT PRIMARY KEY,
        Name TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE Category (
        Id   TEXT PRIMARY KEY,
        Name TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE Seller (
        Id   TEXT PRIMARY KEY,
        Name TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE "Product_new" (
        Id      TEXT PRIMARY KEY,
        Name    TEXT NOT NULL,
        BrandId TEXT REFERENCES Brand (Id)
    )
    """,
    """
    CREATE TABLE "ProductCategory_new" (
        ProductId  TEXT NOT NULL REFERENCES "Product_new" (Id),
        CategoryId TEXT NOT NULL REFERENCES Category (Id),
        PRIMARY KEY (ProductId, CategoryId)
    )
    """,
    """
    CREATE INDEX ix_ProductCategory_CategoryId
        ON "ProductCategory_new" (CategoryId)
    """,
    """
    CREATE TABLE "SellerProduct_new" (
        SellerId    TEXT NOT NULL REFERENCES Seller (Id),
        ProductId   TEXT NOT NULL REFERENCES "Product_new" (Id),
        ExternalSku TEXT NOT NULL,
        PRIMARY KEY (SellerId, ProductId),
        UNIQUE (SellerId, ExternalSku)
    )
    """,
)

# Fixed statements — no string interpolation reaches SQL.
_REFERENCE_SQL = {
    "Brand": (
        "SELECT DISTINCT Brand AS value FROM Product",
        "INSERT INTO Brand (Id, Name) VALUES (:Id, :Name)",
    ),
    "Category": (
        "SELECT DISTINCT Category AS value FROM Product",
        "INSERT INTO Category (Id, Name) VALUES (:Id, :Name)",
    ),
}


def _create_staging_tables(conn: Connection) -> None:
    """Create reference tables and staged target tables for the refactor."""
    for ddl in _STAGING_DDL:
        conn.execute(text(ddl))


def _extract_reference(conn: Connection, column: Literal["Brand", "Category"]) -> dict[str, str]:
    """Extract distinct normalized non-empty ``Product.<column>`` values into ``<column>``.

    Returns ``{normalized_value: new_uuid}``.
    """
    select_sql, insert_sql = _REFERENCE_SQL[column]
    rows = conn.execute(text(select_sql)).all()
    mapping: dict[str, str] = {}
    to_insert: list[dict[str, str]] = []
    for (raw,) in rows:
        norm = normalize(raw)
        if not norm or norm in mapping:
            continue
        new_id = new_uuid()
        mapping[norm] = new_id
        to_insert.append({"Id": new_id, "Name": norm.title()})
    if to_insert:
        conn.execute(text(insert_sql), to_insert)
    logger.info("extracted reference table=%s rows=%d", column, len(to_insert))
    return mapping


def _extract_product_sellers(conn: Connection) -> None:
    """Copy optional legacy ``Product.Seller`` values into ``Seller.Name``.

    The published legacy snapshot stores seller names on ``SellerProduct``. Some
    legacy exports instead also carry a seller column on ``Product``; those values
    are seller entities too, even though the target model does not retain the
    denormalized product column.
    """
    product_columns = {column["name"] for column in inspect(conn).get_columns("Product")}
    if "Seller" not in product_columns:
        logger.info("extracted product sellers rows=0 column=absent")
        return

    existing = {name for (name,) in conn.execute(text("SELECT Name FROM Seller")).all()}
    seller_rows: list[dict[str, str]] = []
    for (raw_name,) in conn.execute(text("SELECT Seller FROM Product")).all():
        if not isinstance(raw_name, str):
            continue
        name = raw_name.strip()
        if not name or name in existing:
            continue
        existing.add(name)
        seller_rows.append({"Id": new_uuid(), "Name": name})

    if seller_rows:
        conn.execute(text("INSERT INTO Seller (Id, Name) VALUES (:Id, :Name)"), seller_rows)
    logger.info("extracted product sellers rows=%d", len(seller_rows))


def _rebuild_product(
    conn: Connection,
    brand_map: dict[str, str],
    category_map: dict[str, str],
) -> dict[int, str]:
    """Copy every legacy ``Product`` row into ``Product_new`` with a fresh ``uuid4`` id
    and ``Brand`` text replaced by a nullable FK. Legacy categories are copied into
    ``ProductCategory_new`` as one association per product when present.

    Returns ``{old_int_id: new_uuid}``.
    """
    rows = conn.execute(text("SELECT Id, Name, Brand, Category FROM Product")).all()
    id_map: dict[int, str] = {}
    batch: list[dict[str, str | None]] = []
    category_links: list[dict[str, str]] = []
    for old_id, name, brand, category in rows:
        new_id = new_uuid()
        id_map[old_id] = new_id
        category_id = category_map.get(normalize(category))
        batch.append(
            {
                "Id": new_id,
                "Name": name,
                "BrandId": brand_map.get(normalize(brand)),
            }
        )
        if category_id:
            category_links.append({"ProductId": new_id, "CategoryId": category_id})
    if batch:
        conn.execute(
            text('INSERT INTO "Product_new" (Id, Name, BrandId) VALUES (:Id, :Name, :BrandId)'),
            batch,
        )
    if category_links:
        conn.execute(
            text(
                'INSERT INTO "ProductCategory_new" (ProductId, CategoryId) '
                "VALUES (:ProductId, :CategoryId)"
            ),
            category_links,
        )
    logger.info("rebuilt Product rows=%d", len(batch))
    return id_map


def _rebuild_seller_product(conn: Connection, product_id_map: dict[int, str]) -> None:
    """Extract ``SellerName`` into ``Seller`` and copy legacy ``SellerProduct`` rows into
    ``SellerProduct_new`` with remapped UUID foreign keys and the integer
    ``SellerProductId`` cast to opaque ``ExternalSku`` text.

    The published base table is empty; this stays correct if it is not.
    """
    rows = conn.execute(
        text("SELECT SellerName, ProductId, SellerProductId FROM SellerProduct")
    ).all()
    seller_map = {
        name: seller_id
        for seller_id, name in conn.execute(text("SELECT Id, Name FROM Seller")).all()
    }
    seller_rows: list[dict[str, str]] = []
    link_rows: list[dict[str, str]] = []
    for seller_name, old_product_id, sku in rows:
        seller_id = seller_map.get(seller_name)
        if seller_id is None:
            seller_id = new_uuid()
            seller_map[seller_name] = seller_id
            seller_rows.append({"Id": seller_id, "Name": seller_name})
        link_rows.append(
            {
                "SellerId": seller_id,
                "ProductId": product_id_map[old_product_id],
                "ExternalSku": str(sku),
            }
        )
    if seller_rows:
        conn.execute(text("INSERT INTO Seller (Id, Name) VALUES (:Id, :Name)"), seller_rows)
    if link_rows:
        conn.execute(
            text(
                'INSERT OR IGNORE INTO "SellerProduct_new" (SellerId, ProductId, ExternalSku) '
                "VALUES (:SellerId, :ProductId, :ExternalSku)"
            ),
            link_rows,
        )
    logger.info("rebuilt SellerProduct sellers=%d links=%d", len(seller_rows), len(link_rows))


def _swap_tables(conn: Connection) -> None:
    """Drop the legacy tables and rename the staging tables into place.

    Also clears the ``sqlite_sequence`` bookkeeping table left behind by the legacy
    ``AUTOINCREMENT`` columns — SQLite forbids dropping it, but the target schema has
    no ``AUTOINCREMENT`` so it must hold no counters.
    """
    conn.execute(text("DROP TABLE SellerProduct"))
    conn.execute(text("DROP TABLE Product"))
    conn.execute(text('ALTER TABLE "Product_new" RENAME TO "Product"'))
    conn.execute(text('ALTER TABLE "ProductCategory_new" RENAME TO "ProductCategory"'))
    conn.execute(text('ALTER TABLE "SellerProduct_new" RENAME TO "SellerProduct"'))
    if conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'")
    ).first():
        conn.execute(text("DELETE FROM sqlite_sequence"))


def _foreign_key_check(conn: Connection) -> None:
    """Raise ``RuntimeError`` if ``PRAGMA foreign_key_check`` reports any violation."""
    violations = conn.execute(text("PRAGMA foreign_key_check")).all()
    if violations:
        raise RuntimeError(f"foreign_key_check failed: {violations!r}")
