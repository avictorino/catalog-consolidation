from __future__ import annotations

import io
import json

import boto3
import pytest
import responses
from moto import mock_aws

from consolidation import infrastructure
from consolidation.infrastructure import (
    DifflibSimilarity,
    FeedError,
    FeedValidationError,
    HttpByteSource,
    ProductEntry,
    S3ByteSource,
    build_source,
    iter_entries,
    iter_feed,
    parse_s3_ref,
    screen_entry,
)

_HTTP = HttpByteSource()


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
    assert [entry.Name for entry in iter_feed(url, _HTTP)] == ["Câmera Canon EOS R6"]


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("s3://my-bucket/path/to/ProductEntry.json", ("my-bucket", "path/to/ProductEntry.json")),
        (
            "https://my-bucket.s3.us-east-1.amazonaws.com/ProductEntry.json",
            ("my-bucket", "ProductEntry.json"),
        ),
        (
            "https://s3.us-east-1.amazonaws.com/my-bucket/nested/key.json",
            ("my-bucket", "nested/key.json"),
        ),
    ],
)
def test_parse_s3_ref(ref: str, expected: tuple[str, str]) -> None:
    assert parse_s3_ref(ref) == expected


def test_parse_s3_ref_rejects_non_s3() -> None:
    with pytest.raises(ValueError, match="not an S3 reference"):
        parse_s3_ref("https://example.com/ProductEntry.json")


def test_build_source_dispatch() -> None:
    assert isinstance(build_source("http"), HttpByteSource)
    assert isinstance(build_source("s3"), S3ByteSource)
    with pytest.raises(ValueError, match="unknown source"):
        build_source("ftp")


@mock_aws
def test_iter_feed_streams_s3_object() -> None:
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="feeds")
    client.put_bucket_policy(
        Bucket="feeds",
        Policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": "*",
                        "Action": "s3:GetObject",
                        "Resource": "arn:aws:s3:::feeds/*",
                    }
                ],
            }
        ),
    )
    client.put_object(
        Bucket="feeds",
        Key="ProductEntry.json",
        Body=json.dumps([_entry(), _entry(Name="Router WiFi 6")], ensure_ascii=False).encode(),
    )
    ref = "s3://feeds/ProductEntry.json"
    assert [e.Name for e in iter_feed(ref, S3ByteSource())] == [
        "Câmera Canon EOS R6",
        "Router WiFi 6",
    ]


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


def test_screen_entry_rejects_injection_and_records_truncated_value(caplog) -> None:
    entry = ProductEntry.model_validate(_entry(Brand="TestBrand'; SELECT 1; --" * 10))
    finding = screen_entry(entry, 4)
    assert finding is not None
    assert len(finding.value) == 120
    assert "event=sqli_attempt" in caplog.records[0].message


def test_screen_entry_allows_benign_apostrophes() -> None:
    entry = ProductEntry.model_validate(_entry(Brand="Levi's", Name="iPad Pro 12.9''"))
    assert screen_entry(entry, 0) is None


# --------------------------------------------------------------------------- #
# Threshold: not a parameter anywhere upstream — the similarity backend
# resolves it lazily, from THRESHOLD in .env, on first access.
# --------------------------------------------------------------------------- #
def test_threshold_falls_back_to_suggested_default_without_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(infrastructure, "_ENV_PATH", tmp_path / "missing.env")
    assert DifflibSimilarity().threshold == DifflibSimilarity.suggested_threshold


def test_threshold_is_read_from_env_on_first_use(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("THRESHOLD=0.75\n", encoding="utf-8")
    monkeypatch.setattr(infrastructure, "_ENV_PATH", env_path)
    assert DifflibSimilarity().threshold == pytest.approx(0.75)


def test_threshold_is_cached_after_first_access(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("THRESHOLD=0.75\n", encoding="utf-8")
    monkeypatch.setattr(infrastructure, "_ENV_PATH", env_path)
    similarity = DifflibSimilarity()
    assert similarity.threshold == pytest.approx(0.75)
    env_path.write_text("THRESHOLD=0.10\n", encoding="utf-8")
    assert similarity.threshold == pytest.approx(0.75)  # unchanged: resolved once


def test_explicit_threshold_overrides_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("THRESHOLD=0.75\n", encoding="utf-8")
    monkeypatch.setattr(infrastructure, "_ENV_PATH", env_path)
    assert DifflibSimilarity(0.91).threshold == pytest.approx(0.91)


@pytest.mark.parametrize("bad", ["1.5", "-0.1", "nope"])
def test_bad_threshold_in_env_raises(monkeypatch: pytest.MonkeyPatch, tmp_path, bad: str) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(f"THRESHOLD={bad}\n", encoding="utf-8")
    monkeypatch.setattr(infrastructure, "_ENV_PATH", env_path)
    with pytest.raises(ValueError, match="THRESHOLD"):
        _ = DifflibSimilarity().threshold
