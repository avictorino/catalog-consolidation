from __future__ import annotations

from pathlib import Path

import pytest
import requests
import responses

from consolidation.download import download_to, verify_sqlite_header

URL = "https://example.com/catalog.db"
SQLITE_BYTES = b"SQLite format 3\x00" + b"\x00" * 512


@responses.activate
def test_download_writes_temp_file(tmp_path: Path) -> None:
    responses.add(responses.GET, URL, body=SQLITE_BYTES, status=200)
    path = download_to(URL, tmp_path)
    assert path.parent == tmp_path
    assert path.read_bytes() == SQLITE_BYTES
    verify_sqlite_header(path)


@responses.activate
def test_http_error_raises_and_leaves_no_temp(tmp_path: Path) -> None:
    responses.add(responses.GET, URL, status=404)
    with pytest.raises(requests.HTTPError):
        download_to(URL, tmp_path)
    assert list(tmp_path.glob("*.tmp")) == []


@responses.activate
def test_non_sqlite_body_rejected_by_header_check(tmp_path: Path) -> None:
    responses.add(responses.GET, URL, body=b"<!DOCTYPE html><html>nope</html>", status=200)
    path = download_to(URL, tmp_path)
    with pytest.raises(ValueError, match="not a SQLite database"):
        verify_sqlite_header(path)


def test_verify_header_on_real_sqlite(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "real.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    verify_sqlite_header(db)
