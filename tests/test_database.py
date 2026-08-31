from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import create_engine

from consolidation import database


def _classify(db_path: Path) -> str:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            return database.classify_source(conn)
    finally:
        engine.dispose()


def test_classify_legacy(legacy_db: Path) -> None:
    assert _classify(legacy_db) == "legacy"


def test_classify_migrated(migrated_db: Path) -> None:
    assert _classify(migrated_db) == "migrated"


def test_classify_unrecognized_empty(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    assert _classify(db) == "unrecognized"


def test_classify_unrecognized_partial(tmp_path: Path) -> None:
    db = tmp_path / "weird.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE Product (Id INTEGER PRIMARY KEY, Name TEXT); CREATE TABLE Other (x);"
    )
    conn.close()
    assert _classify(db) == "unrecognized"


def test_new_uuid_shape() -> None:
    value = database.new_uuid()
    assert len(value) == 36
    assert value.count("-") == 4
