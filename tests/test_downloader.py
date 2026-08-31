from __future__ import annotations

from pathlib import Path

import pytest
import requests
import responses

from consolidation.downloader import download_to

_URL = "https://example.com/catalog.db"


@responses.activate
def test_download_writes_temp_file(tmp_path: Path) -> None:
    body = b"SQLite format 3\x00" + b"\x00" * 512
    responses.add(responses.GET, _URL, body=body, status=200)
    path = download_to(_URL, tmp_path)
    assert path.parent == tmp_path
    assert path.read_bytes() == body


@responses.activate
def test_download_http_error_leaves_no_temp(tmp_path: Path) -> None:
    responses.add(responses.GET, _URL, status=404)
    with pytest.raises(requests.HTTPError):
        download_to(_URL, tmp_path)
    assert list(tmp_path.glob("*.tmp")) == []


@responses.activate
def test_download_partial_failure_leaves_no_temp(tmp_path: Path) -> None:
    responses.add(responses.GET, _URL, body=requests.ConnectionError("boom"))
    with pytest.raises(requests.ConnectionError):
        download_to(_URL, tmp_path)
    assert list(tmp_path.glob("*.tmp")) == []
