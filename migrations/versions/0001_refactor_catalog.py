"""Refactor the legacy catalog into the normalized model.

Drops the two denormalized tables and rebuilds them alongside three reference tables
(``Brand``, ``Category``, ``Seller``), every primary key a Python-minted ``uuid4``
``TEXT``. Target schema and steps: ``spec/data-profile.md#refactored-database``.

One-way: an already-migrated database carries this revision in ``alembic_version`` and
``upgrade head`` is a no-op.

Revision ID: 0001
Revises:
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from consolidation import infrastructure

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()

    infrastructure.create_staging_tables(conn)
    brand_map = infrastructure.extract_brands(conn)
    category_map = infrastructure.extract_categories(conn)
    infrastructure.extract_product_sellers(conn)
    product_id_map = infrastructure.rebuild_product(conn, brand_map, category_map)
    infrastructure.rebuild_seller_product(conn, product_id_map)
    infrastructure.swap_tables(conn)
    infrastructure.foreign_key_check(conn)


def downgrade() -> None:
    raise NotImplementedError("the catalog refactor is one-way")
