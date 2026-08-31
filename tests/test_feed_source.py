from __future__ import annotations

import io
import json

import pytest
import responses

from consolidation.entries import ProductEntry
from consolidation.feed_source import FeedError, FeedValidationError, iter_entries, iter_feed


class _ChunkedStream:
    def __init__(self, value: bytes, chunk_size: int) -> None:
        self.value = value
        self.chunk_size = chunk_size
        self.position = 0

    def read(self, size: int = -1) -> bytes:
        if self.position >= len(self.value):
            return b""
        end = min(self.position + self.chunk_size, len(self.value))
        chunk = self.value[self.position : end]
        self.position = end
        return chunk


def _entry(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "Id": "sku-1",
        "SellerName": "GardenStore",
        "Name": "Câmera Canon EOS R6",
        "Brand": "Canon",
        "Category": "Photography",
    }
    value.update(overrides)
    return value


def test_iter_entries_streams_chunked_utf8() -> None:
    payload = json.dumps([_entry()], ensure_ascii=False).encode()
    entries = list(iter_entries(_ChunkedStream(payload, 3)))
    assert entries == [ProductEntry.model_validate(_entry())]


@responses.activate
def test_iter_feed_streams_http_response() -> None:
    url = "https://example.com/ProductEntry.json"
    responses.add(
        responses.GET,
        url,
        body=json.dumps([_entry()], ensure_ascii=False).encode(),
        status=200,
    )
    assert [entry.Name for entry in iter_feed(url)] == ["Câmera Canon EOS R6"]


def test_iter_entries_ignores_unknown_fields() -> None:
    entries = list(iter_entries(io.BytesIO(json.dumps([_entry(extra="ignored")]).encode())))
    assert entries[0].Name == "Câmera Canon EOS R6"


def test_iter_entries_rejects_non_array_root() -> None:
    with pytest.raises(FeedError, match="root must be a JSON array"):
        list(iter_entries(io.BytesIO(json.dumps(_entry()).encode())))


def test_iter_entries_reports_invalid_fields_without_record_contents() -> None:
    with pytest.raises(FeedValidationError) as exc_info:
        list(iter_entries(io.BytesIO(json.dumps([_entry(Name=" ")]).encode())))
    assert exc_info.value.record_index == 0
    assert exc_info.value.fields == ("Name",)
    assert "Câmera" not in str(exc_info.value)
