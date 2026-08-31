from __future__ import annotations

import logging
import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.engine import Connection

from consolidation import pipeline, repository, schema
from consolidation.feed import ProductEntry
from consolidation.repository import FeedImporter

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
    assert _count(migrated_db, "ProductCategory") == 4
    assert _count(migrated_db, "Seller") == 0
    assert _count(migrated_db, "SellerProduct") == 0
    assert [column[1] for column in _rows(migrated_db, "PRAGMA table_info(Product)")] == [
        "Id",
        "Name",
        "BrandId",
    ]


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
    assert names == ["Black Decker", "Canon", "Ikea"]
    # the two BLACK+DECKER spellings collapse onto one brand row
    linked = _rows(
        migrated_db,
        "SELECT p.Name FROM Product p JOIN Brand b ON p.BrandId = b.Id "
        "WHERE b.Name = 'Black Decker'",
    )
    assert {n for (n,) in linked} == {"Cordless Drill", "Impact Driver"}


def test_null_fks_preserved(migrated_db: Path) -> None:
    assert _count(migrated_db, "Product") == PRODUCT_ROWS
    assert _rows(migrated_db, "SELECT count(*) FROM Product WHERE BrandId IS NULL") == [(2,)]
    assert _rows(
        migrated_db,
        "SELECT count(*) FROM Product p "
        "WHERE NOT EXISTS (SELECT 1 FROM ProductCategory pc WHERE pc.ProductId = p.Id)",
    ) == [(2,)]


def test_rerun_is_noop(migrated_db: Path) -> None:
    before = _rows(migrated_db, "SELECT Id, Name, BrandId FROM Product ORDER BY Id")
    before_categories = _rows(
        migrated_db,
        "SELECT ProductId, CategoryId FROM ProductCategory " "ORDER BY ProductId, CategoryId",
    )
    apply_refactor(migrated_db)  # second time: source is 'migrated', upgrade head no-ops
    after = _rows(migrated_db, "SELECT Id, Name, BrandId FROM Product ORDER BY Id")
    after_categories = _rows(
        migrated_db,
        "SELECT ProductId, CategoryId FROM ProductCategory " "ORDER BY ProductId, CategoryId",
    )
    assert before == after
    assert before_categories == after_categories


def test_product_seller_values_are_copied_to_seller(tmp_path: Path) -> None:
    db = tmp_path / "product-seller.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE Product (
                Id INTEGER PRIMARY KEY AUTOINCREMENT,
                Name TEXT NOT NULL,
                Brand TEXT,
                Category TEXT,
                Seller TEXT
            );
            CREATE TABLE SellerProduct (
                Id INTEGER PRIMARY KEY AUTOINCREMENT,
                SellerName TEXT NOT NULL,
                ProductId INTEGER NOT NULL REFERENCES Product (Id),
                SellerProductId INTEGER NOT NULL
            );
            INSERT INTO Product (Name, Seller) VALUES
                ('Product A', 'Product Seller'),
                ('Product B', 'Product Seller'),
                ('Product C', '  Another Seller  '),
                ('Product D', NULL),
                ('Product E', '');
            INSERT INTO SellerProduct (SellerName, ProductId, SellerProductId)
                VALUES ('Link Seller', 1, 10);
            """
        )
        conn.commit()
    finally:
        conn.close()

    apply_refactor(db)

    assert _rows(db, "SELECT Name FROM Seller ORDER BY Name") == [
        ("Another Seller",),
        ("Link Seller",),
        ("Product Seller",),
    ]


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
    monkeypatch.setattr(pipeline, "iter_feed", lambda _url: iter(()))
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


def test_pipeline_imports_feed(
    _stub_download: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = [
        ProductEntry.model_validate(
            {
                "Id": "sku-1",
                "SellerName": "GardenStore",
                "Name": "Camera Canon EOS R6",
                "Brand": "Canon",
                "Category": "Photo",
            }
        ),
        ProductEntry.model_validate(
            {
                "Id": "sku-threat",
                "SellerName": "MegaStore",
                "Name": "Security Test Product",
                "Brand": "TestBrand'; SELECT 1; --",
                "Category": "Security",
            }
        ),
        ProductEntry.model_validate(
            {
                "Id": "sku-2",
                "SellerName": "GardenStore",
                "Name": "Camera Canon EOS R6",
                "Brand": "Canon",
                "Category": "Photography",
            }
        ),
    ]
    monkeypatch.setattr(pipeline, "iter_feed", lambda _url: iter(entries))
    output = tmp_path / "catalog_output.db"

    assert pipeline.run(**_config(output)) == 0
    assert _count(output, "Product") == PRODUCT_ROWS
    assert _count(output, "Seller") == 1
    assert _count(output, "SellerProduct") == 1


def test_pipeline_isolates_item_failure_and_logs_it(
    _stub_download: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entries = [
        ProductEntry.model_validate(
            {
                "Id": "sku-camera",
                "SellerName": "GardenStore",
                "Name": "Camera Canon EOS R6",
                "Brand": "Canon",
                "Category": "Photography",
            }
        ),
        ProductEntry.model_validate(
            {
                "Id": "sku-fails",
                "SellerName": "GardenStore",
                "Name": "Cordless Drill",
                "Brand": "BLACK+DECKER",
                "Category": "Tools",
            }
        ),
        ProductEntry.model_validate(
            {
                "Id": "sku-driver",
                "SellerName": "GardenStore",
                "Name": "Impact Driver",
                "Brand": "BLACK+DECKER",
                "Category": "Tools",
            }
        ),
    ]
    original_process = FeedImporter.process

    def fail_one_item(self, entry, record_index, report) -> None:
        if record_index == 1:
            raise RuntimeError("injected item failure")
        original_process(self, entry, record_index, report)

    monkeypatch.setattr(pipeline, "iter_feed", lambda _url: iter(entries))
    monkeypatch.setattr(FeedImporter, "process", fail_one_item)
    output = tmp_path / "catalog_output.db"

    with caplog.at_level(logging.ERROR, logger="consolidation"):
        result = pipeline.run(**_config(output))

    assert result == 1
    assert output.exists()
    assert _count(output, "Product") == PRODUCT_ROWS
    assert _count(output, "Seller") == 1
    assert _count(output, "SellerProduct") == 2
    assert any("feed item failures count=1" in record.message for record in caplog.records)
    assert any(
        "record=1" in record.message and "injected item failure" in record.message
        for record in caplog.records
    )


@pytest.mark.parametrize("already_migrated", [False, True], ids=["legacy", "migrated"])
@pytest.mark.parametrize(
    ("table_name", "column"),
    [
        ("Product", "BrandId"),
        ("ProductCategory", "CategoryId"),
        ("SellerProduct", "SellerId"),
        ("SellerProduct", "ProductId"),
    ],
)
def test_pipeline_enforces_foreign_keys_and_rolls_back_failed_item(
    _stub_download: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    already_migrated: bool,
    table_name: str,
    column: str,
) -> None:
    if already_migrated:
        apply_refactor(_stub_download)
    entries = [
        ProductEntry(
            Id="sku-camera",
            SellerName="FirstSeller",
            Name="Camera Canon EOS R6",
            Brand="Canon",
            Category="Photography",
        ),
        ProductEntry(
            Id="sku-failed",
            SellerName="FailedSeller",
            Name="Test Mixer 1001",
            Brand="New Brand",
            Category="New Category",
        ),
        ProductEntry(
            Id="sku-recovered",
            SellerName="RecoveredSeller",
            Name="Test Speaker 2002",
            Brand="New Brand",
            Category="New Category",
        ),
    ]
    original_process = FeedImporter.process
    enforcement = []

    def violate_one_item(self, entry, record_index, report) -> None:
        enforcement.append(self.conn.exec_driver_sql("PRAGMA foreign_keys").scalar())
        original_process(self, entry, record_index, report)
        if record_index == 1:
            table = schema.metadata.tables[table_name]
            if table_name == "Product":
                where = table.c.Name == entry.Name
            elif table_name == "ProductCategory":
                product_id = self.conn.scalar(
                    schema.Product.select()
                    .with_only_columns(schema.Product.c.Id)
                    .where(schema.Product.c.Name == entry.Name)
                )
                where = table.c.ProductId == product_id
            else:
                where = table.c.ExternalSku == entry.Id
            # Fail after all item writes, exercising real SQLite enforcement and rollback.
            self.conn.execute(update(table).where(where).values({column: schema.new_uuid()}))

    monkeypatch.setattr(pipeline, "iter_feed", lambda _url: iter(entries))
    monkeypatch.setattr(FeedImporter, "process", violate_one_item)
    output = tmp_path / "catalog_output.db"

    with caplog.at_level(logging.INFO, logger="consolidation"):
        result = pipeline.run(**_config(output))

    assert result == 1
    assert enforcement == [1, 1, 1]
    assert _count(output, "Product") == PRODUCT_ROWS + 1
    assert _count(output, "Brand") == BRAND_ROWS + 1
    assert _count(output, "Category") == CATEGORY_ROWS + 1
    assert _count(output, "Seller") == 2
    assert _count(output, "SellerProduct") == 2
    assert _rows(output, "SELECT Name FROM Product WHERE Name = 'Test Mixer 1001'") == []
    assert _rows(output, "SELECT Name FROM Seller ORDER BY Name") == [
        ("FirstSeller",),
        ("RecoveredSeller",),
    ]
    assert _rows(output, "PRAGMA foreign_key_check") == []
    assert "processed=3 new=1 linked=2 skipped=0 threat=0 failed=1" in caplog.text
    assert "feed item failures count=1" in caplog.text
    assert any(
        "record=1" in record.message
        and "reason=" in record.message
        and "IntegrityError" in record.message
        and "FOREIGN KEY constraint failed" in record.message
        for record in caplog.records
    )


def test_pipeline_aborts_before_feed_if_foreign_keys_cannot_be_enabled(
    _stub_download: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    output = tmp_path / "catalog_output.db"
    output.write_bytes(b"previous output")
    original_exec = Connection.exec_driver_sql
    feed_requested = False

    def ignore_enabling(self, statement, *args, **kwargs):
        if statement == "PRAGMA foreign_keys = ON":
            statement = "PRAGMA foreign_keys"
        return original_exec(self, statement, *args, **kwargs)

    def unexpected_feed(_url):
        nonlocal feed_requested
        feed_requested = True
        return iter(())

    monkeypatch.setattr(Connection, "exec_driver_sql", ignore_enabling)
    monkeypatch.setattr(pipeline, "iter_feed", unexpected_feed)

    assert pipeline.run(**_config(output)) == 1
    assert not feed_requested
    assert "foreign key enforcement could not be enabled" in caplog.text
    assert output.read_bytes() == b"previous output"
    assert list(tmp_path.glob("*.tmp")) == []


def test_pipeline_rollback_preserves_previous_output(
    _stub_download: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "catalog_output.db"
    output.write_bytes(b"SQLite format 3\x00previous")

    # Let the whole refactor run, then fail before the setup transaction commits:
    # migrate() must roll the pending inserts back and leave the previous output intact.
    real_upgrade = repository.command.upgrade

    def upgrade_then_boom(*args: object, **kwargs: object) -> None:
        real_upgrade(*args, **kwargs)
        raise RuntimeError("injected failure after pending inserts")

    monkeypatch.setattr(repository.command, "upgrade", upgrade_then_boom)
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
