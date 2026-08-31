"""Orchestration tests for ``pipeline.run`` using a fake ``Catalog`` (no SQLite)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from consolidation import pipeline
from consolidation.feed import ProductEntry, Report

ENTRY = ProductEntry(Id="sku-1", SellerName="Seller", Name="Widget", Brand="Acme", Category="Tools")


class FakeImporter:
    def __init__(self, *, fail_on: set[int] | None = None) -> None:
        self.processed: list[int] = []
        self._fail_on = fail_on or set()

    def process(self, entry: ProductEntry, record_index: int, report: Report) -> None:
        if record_index in self._fail_on:
            raise RuntimeError("injected item failure")
        self.processed.append(record_index)
        report.new += 1


class FakeCatalog:
    """In-memory stand-in for ``CatalogRepository`` that records the call sequence."""

    def __init__(
        self,
        _db_path: Path,
        *,
        source: str = "legacy",
        fail_items: set[int] | None = None,
    ) -> None:
        self.source = source
        self.calls: list[str] = []
        self.importer = FakeImporter(fail_on=fail_items)

    def __enter__(self) -> FakeCatalog:
        self.calls.append("enter")
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("exit")

    def classify_source(self) -> str:
        self.calls.append("classify")
        return self.source

    def migrate(self) -> None:
        self.calls.append("migrate")

    def enable_foreign_keys(self) -> None:
        self.calls.append("enable_fk")

    def prepare_import(self, *, matcher: str, threshold: float) -> None:
        self.calls.append("prepare_import")

    @contextmanager
    def item_transaction(self):
        self.calls.append("item_txn")
        yield self.importer


@pytest.fixture
def _stub_io(monkeypatch: pytest.MonkeyPatch):
    def fake_download_to(url: str, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp = dest_dir / ".catalog-stub.db.tmp"
        tmp.write_bytes(b"SQLite format 3\x00stub")
        return tmp

    monkeypatch.setattr(pipeline, "download_to", fake_download_to)
    monkeypatch.setattr(pipeline, "verify_sqlite_header", lambda _path: None)
    monkeypatch.setattr(pipeline, "iter_feed", lambda _url: iter([ENTRY, ENTRY, ENTRY]))


def _run(output: Path, factory) -> int:
    return pipeline.run(
        catalog_url="https://example.com/catalog.db",
        products_url="https://example.com/feed.json",
        output=output,
        matcher="difflib",
        threshold=0.9,
        repository_factory=factory,
    )


def test_happy_path_runs_stages_in_order_then_publishes(_stub_io, tmp_path: Path) -> None:
    seen: list[FakeCatalog] = []

    def factory(db_path: Path) -> FakeCatalog:
        repo = FakeCatalog(db_path)
        seen.append(repo)
        return repo

    output = tmp_path / "out" / "catalog_output.db"
    assert _run(output, factory) == 0
    assert output.read_bytes() == b"SQLite format 3\x00stub"
    assert seen[0].calls == [
        "enter",
        "classify",
        "migrate",
        "enable_fk",
        "prepare_import",
        "item_txn",
        "item_txn",
        "item_txn",
        "exit",
    ]
    assert seen[0].importer.processed == [0, 1, 2]


def test_unrecognized_source_aborts_before_migrate(_stub_io, tmp_path: Path) -> None:
    seen: list[FakeCatalog] = []

    def factory(db_path: Path) -> FakeCatalog:
        repo = FakeCatalog(db_path, source="unrecognized")
        seen.append(repo)
        return repo

    output = tmp_path / "out.db"
    assert _run(output, factory) == 1
    assert not output.exists()
    assert seen[0].calls == ["enter", "classify", "exit"]


def test_item_failure_is_isolated_and_yields_nonzero_exit(_stub_io, tmp_path: Path) -> None:
    def factory(db_path: Path) -> FakeCatalog:
        return FakeCatalog(db_path, fail_items={1})

    output = tmp_path / "out.db"
    # published (exists) but exit code 1 because one item failed
    assert _run(output, factory) == 1
    assert output.exists()
