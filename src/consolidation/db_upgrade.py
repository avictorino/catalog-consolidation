"""Schema upgrade: target schema, source classification, and the refactor steps.

Everything that reshapes the database lives here — the declarative target schema
(SQLAlchemy Core ``Table`` metadata), the introspection that classifies a downloaded
database, and the data-migration helpers the Alembic revision calls to build the new
model, copy data between tables, and drop the denormalized columns.

The full target schema and the migration steps are specified in
``spec/data-profile.md#refactored-database``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Literal

from sqlalchemy import (
    Column,
    ForeignKey,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    inspect,
    text,
)
from sqlalchemy.engine import Connection

from consolidation.util import normalize

logger = logging.getLogger("consolidation")

SourceKind = Literal["legacy", "migrated", "unrecognized"]

TARGET_TABLES = ("Brand", "Category", "Product", "Seller", "SellerProduct")

# --------------------------------------------------------------------------- #
# Target schema (canonical names). Used for introspection and by the feed
# import; the refactor below builds the same shape via staged tables.
# --------------------------------------------------------------------------- #
metadata = MetaData()

Brand = Table(
    "Brand",
    metadata,
    Column("Id", String, primary_key=True),  # uuid4
    Column("Name", String, nullable=False, unique=True),  # capitalized normalized name
)

Category = Table(
    "Category",
    metadata,
    Column("Id", String, primary_key=True),  # uuid4
    Column("Name", String, nullable=False, unique=True),  # capitalized normalized name
)

Seller = Table(
    "Seller",
    metadata,
    Column("Id", String, primary_key=True),  # uuid4
    Column("Name", String, nullable=False, unique=True),
)

Product = Table(
    "Product",
    metadata,
    Column("Id", String, primary_key=True),  # uuid4
    Column("Name", String, nullable=False),
    Column("BrandId", String, ForeignKey("Brand.Id")),  # nullable
    Column("CategoryId", String, ForeignKey("Category.Id")),  # nullable
)

SellerProduct = Table(
    "SellerProduct",
    metadata,
    Column("SellerId", String, ForeignKey("Seller.Id"), primary_key=True, nullable=False),
    Column("ProductId", String, ForeignKey("Product.Id"), primary_key=True, nullable=False),
    Column("ExternalSku", String, nullable=False),  # the feed entry's Id (opaque)
    UniqueConstraint("SellerId", "ExternalSku"),
)


def new_uuid() -> str:
    """A fresh ``uuid4`` as a 36-char string, minted in Python (no DB coordination)."""
    return str(uuid.uuid4())


# --------------------------------------------------------------------------- #
# Source classification (replaces the old PRAGMA user_version guard: Alembic's
# alembic_version table is the marker now).
# --------------------------------------------------------------------------- #
def classify_source(conn: Connection) -> SourceKind:
    """Classify a downloaded database as ``legacy``, ``migrated`` or ``unrecognized``."""
    insp = inspect(conn)
    tables = set(insp.get_table_names())
    if not {"Product", "SellerProduct"} <= tables:
        return "unrecognized"

    product_cols = {c["name"]: c for c in insp.get_columns("Product")}
    sp_cols = {c["name"] for c in insp.get_columns("SellerProduct")}
    product_id_type = str(product_cols.get("Id", {}).get("type", "")).upper()

    legacy = (
        {"Brand", "Category"} <= product_cols.keys()
        and "BrandId" not in product_cols
        and product_id_type.startswith("INTEGER")
        and {"SellerName", "SellerProductId"} <= sp_cols
        and "ExternalSku" not in sp_cols
        and not {"Brand", "Category", "Seller", "alembic_version"} & tables
    )
    if legacy:
        return "legacy"

    migrated = (
        {"Brand", "Category", "Seller", "alembic_version"} <= tables
        and {"BrandId", "CategoryId"} <= product_cols.keys()
        and "TEXT" in product_id_type
        and "ExternalSku" in sp_cols
    )
    if migrated:
        return "migrated"

    return "unrecognized"


# --------------------------------------------------------------------------- #
# Refactor steps (called by migrations/versions/0001_refactor_catalog.py).
#
# SQLite quirk: PRAGMA foreign_keys is a no-op inside a transaction, and the
# whole refactor runs in the single import transaction. We therefore never
# enable FK enforcement during the rebuild (SQLAlchemy leaves it off by
# default) and validate with PRAGMA foreign_key_check at the end.
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
        Id         TEXT PRIMARY KEY,
        Name       TEXT NOT NULL,
        BrandId    TEXT REFERENCES Brand (Id),
        CategoryId TEXT REFERENCES Category (Id)
    )
    """,
    """
    CREATE TABLE "SellerProduct_new" (
        SellerId    TEXT NOT NULL REFERENCES Seller (Id),
        ProductId   TEXT NOT NULL REFERENCES Product (Id),
        ExternalSku TEXT NOT NULL,
        PRIMARY KEY (SellerId, ProductId),
        UNIQUE (SellerId, ExternalSku)
    )
    """,
)


def create_staging_tables(conn: Connection) -> None:
    """Create ``Brand`` / ``Category`` / ``Seller`` and the two ``*_new`` staging tables."""
    for ddl in _STAGING_DDL:
        conn.execute(text(ddl))


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


def extract_brands(conn: Connection) -> dict[str, str]:
    return _extract_reference(conn, "Brand")


def extract_categories(conn: Connection) -> dict[str, str]:
    return _extract_reference(conn, "Category")


def rebuild_product(
    conn: Connection,
    brand_map: dict[str, str],
    category_map: dict[str, str],
) -> dict[int, str]:
    """Copy every legacy ``Product`` row into ``Product_new`` with a fresh ``uuid4`` id
    and ``Brand`` / ``Category`` text replaced by nullable FKs.

    Returns ``{old_int_id: new_uuid}``.
    """
    rows = conn.execute(text("SELECT Id, Name, Brand, Category FROM Product")).all()
    id_map: dict[int, str] = {}
    batch: list[dict[str, str | None]] = []
    for old_id, name, brand, category in rows:
        new_id = new_uuid()
        id_map[old_id] = new_id
        batch.append(
            {
                "Id": new_id,
                "Name": name,
                "BrandId": brand_map.get(normalize(brand)),
                "CategoryId": category_map.get(normalize(category)),
            }
        )
    if batch:
        conn.execute(
            text(
                'INSERT INTO "Product_new" (Id, Name, BrandId, CategoryId) '
                "VALUES (:Id, :Name, :BrandId, :CategoryId)"
            ),
            batch,
        )
    logger.info("rebuilt Product rows=%d", len(batch))
    return id_map


def rebuild_seller_product(conn: Connection, product_id_map: dict[int, str]) -> None:
    """Extract ``SellerName`` into ``Seller`` and copy legacy ``SellerProduct`` rows into
    ``SellerProduct_new`` with remapped UUID foreign keys and the integer
    ``SellerProductId`` cast to opaque ``ExternalSku`` text.

    The published base table is empty; this stays correct if it is not.
    """
    rows = conn.execute(
        text("SELECT SellerName, ProductId, SellerProductId FROM SellerProduct")
    ).all()
    seller_map: dict[str, str] = {}
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


def swap_tables(conn: Connection) -> None:
    """Drop the legacy tables and rename the staging tables into place.

    Also clears the ``sqlite_sequence`` bookkeeping table left behind by the legacy
    ``AUTOINCREMENT`` columns — SQLite forbids dropping it, but the target schema has
    no ``AUTOINCREMENT`` so it must hold no counters.
    """
    conn.execute(text("DROP TABLE SellerProduct"))
    conn.execute(text("DROP TABLE Product"))
    conn.execute(text('ALTER TABLE "Product_new" RENAME TO "Product"'))
    conn.execute(text('ALTER TABLE "SellerProduct_new" RENAME TO "SellerProduct"'))
    if conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'")
    ).first():
        conn.execute(text("DELETE FROM sqlite_sequence"))


def foreign_key_check(conn: Connection) -> None:
    """Raise ``RuntimeError`` if ``PRAGMA foreign_key_check`` reports any violation."""
    violations = conn.execute(text("PRAGMA foreign_key_check")).all()
    if violations:
        raise RuntimeError(f"foreign_key_check failed: {violations!r}")
