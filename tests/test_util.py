from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import requests
import responses

from consolidation.domain import normalize
from consolidation.infrastructure import download_to, verify_sqlite_header

# --------------------------------------------------------------------------- #
# normalize
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Smartphone  Galaxy S23", "smartphone galaxy s23"),
        ("Câmera Canon EOS R6", "camera canon eos r6"),
        ("Camera Canon EOS R6", "camera canon eos r6"),
        ('iPad Pro 12.9"', "ipad pro 12 9"),
        ("iPad Pro 12.9''", "ipad pro 12 9"),
        ("iPad Pro 12.9", "ipad pro 12 9"),
        ("BLACK+DECKER", "black decker"),
        ("Black+Decker", "black decker"),
        ("Simplehuman", "simplehuman"),
        ("simplehuman", "simplehuman"),
        ("Levi's", "levis"),
        ("Levis", "levis"),
        ("Levi’s", "levis"),  # noqa: RUF001 -- curly apostrophe is the point
        ("  trailing and leading  ", "trailing and leading"),
        (None, ""),
        ("", ""),
        ("!!!", ""),
    ],
)
def test_normalize(raw: str | None, expected: str) -> None:
    assert normalize(raw) == expected


def test_normalize_is_idempotent() -> None:
    once = normalize('Câmera  Canon EOS R6 12.9"')
    assert normalize(once) == once


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #

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
    db = tmp_path / "real.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    verify_sqlite_header(db)
