from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from consolidation import db_upgrade, pipeline

from .conftest import BRAND_ROWS, CATEGORY_ROWS, PRODUCT_ROWS, apply_refactor


def _rows(db: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _count(db: Path, table: str) -> int:
    return _rows(db, f'SELECT count(*) FROM "{table}"')[0][0]  # noqa: S608 -- fixed test table names


def test_target_schema_and_counts(migrated_db: Path) -> None:
    assert _count(migrated_db, "Brand") == BRAND_ROWS
    assert _count(migrated_db, "Category") == CATEGORY_ROWS
    assert _count(migrated_db, "Product") == PRODUCT_ROWS
    assert _count(migrated_db, "Seller") == 0
    assert _count(migrated_db, "SellerProduct") == 0


def test_foreign_keys_and_version(migrated_db: Path) -> None:
    assert _rows(migrated_db, "PRAGMA foreign_key_check") == []
    assert _rows(migrated_db, "SELECT version_num FROM alembic_version") == [("0001",)]


def test_no_autoincrement_left(migrated_db: Path) -> None:
    ddl = " ".join(r[0] or "" for r in _rows(migrated_db, "SELECT sql FROM sqlite_master"))
    assert "AUTOINCREMENT" not in ddl.upper()
    assert _rows(migrated_db, "SELECT * FROM sqlite_sequence") == []


def test_product_ids_are_uuids_and_names_preserved(legacy_db: Path) -> None:
    before = {name for (name,) in _rows(legacy_db, "SELECT Name FROM Product")}
    apply_refactor(legacy_db)
    after = _rows(legacy_db, "SELECT Id, Name FROM Product")
    assert {name for _, name in after} == before
    assert all(len(pid) == 36 for pid, _ in after)


def test_brand_merge_on_normalization(migrated_db: Path) -> None:
    names = sorted(n for (n,) in _rows(migrated_db, "SELECT Name FROM Brand"))
    assert names == ["black decker", "canon", "ikea"]
    # the two BLACK+DECKER spellings collapse onto one brand row
    linked = _rows(
        migrated_db,
        "SELECT p.Name FROM Product p JOIN Brand b ON p.BrandId = b.Id "
        "WHERE b.Name = 'black decker'",
    )
    assert {n for (n,) in linked} == {"Cordless Drill", "Impact Driver"}


def test_null_fks_preserved(migrated_db: Path) -> None:
    assert _count(migrated_db, "Product") == PRODUCT_ROWS
    assert _rows(migrated_db, "SELECT count(*) FROM Product WHERE BrandId IS NULL") == [(2,)]
    assert _rows(migrated_db, "SELECT count(*) FROM Product WHERE CategoryId IS NULL") == [(2,)]


def test_rerun_is_noop(migrated_db: Path) -> None:
    before = _rows(migrated_db, "SELECT Id, Name, BrandId, CategoryId FROM Product ORDER BY Id")
    apply_refactor(migrated_db)  # second time: source is 'migrated', upgrade head no-ops
    after = _rows(migrated_db, "SELECT Id, Name, BrandId, CategoryId FROM Product ORDER BY Id")
    assert before == after


# --------------------------------------------------------------------------- #
# Pipeline-level: download is stubbed to copy the fixture into place.
# --------------------------------------------------------------------------- #
@pytest.fixture
def _stub_download(monkeypatch: pytest.MonkeyPatch, legacy_db: Path):
    def fake_download_to(url: str, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp = dest_dir / ".catalog-stub.db.tmp"
        shutil.copy(legacy_db, tmp)
        return tmp

    monkeypatch.setattr(pipeline, "download_to", fake_download_to)
    return legacy_db


def _config(output: Path) -> dict[str, object]:
    return {
        "catalog_url": "https://example.com/catalog.db",
        "products_url": "https://example.com/ProductEntry.json",
        "output": output,
        "matcher": "difflib",
        "threshold": 0.90,
    }


def test_pipeline_publishes_refactored_output(_stub_download: Path, tmp_path: Path) -> None:
    output = tmp_path / "out" / "catalog_output.db"
    assert pipeline.run(**_config(output)) == 0
    assert output.exists()
    assert _count(output, "Product") == PRODUCT_ROWS
    assert _rows(output, "PRAGMA foreign_key_check") == []
    assert list((tmp_path / "out").glob("*.tmp")) == []


def test_pipeline_rollback_preserves_previous_output(
    _stub_download: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "catalog_output.db"
    output.write_bytes(b"SQLite format 3\x00previous")

    def boom(conn) -> None:
        raise RuntimeError("injected failure after pending inserts")

    monkeypatch.setattr(db_upgrade, "foreign_key_check", boom)
    assert pipeline.run(**_config(output)) == 1
    assert output.read_bytes() == b"SQLite format 3\x00previous"
    assert list(tmp_path.glob("*.tmp")) == []


def test_pipeline_aborts_on_unrecognized_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()

    def fake_download_to(url: str, dest_dir: Path) -> Path:
        tmp = dest_dir / ".catalog-stub.db.tmp"
        shutil.copy(empty, tmp)
        return tmp

    monkeypatch.setattr(pipeline, "download_to", fake_download_to)
    output = tmp_path / "out.db"
    assert pipeline.run(**_config(output)) == 1
    assert not output.exists()
