"""Streaming seller-feed parsing, validation, and SQL injection screening."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import BinaryIO

import ijson
import libinjection
import requests
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

logger = logging.getLogger("consolidation")

_TIMEOUT = (10, 60)
_PROBE_SIZE = 64 * 1024
_STRING_FIELDS = ("Id", "SellerName", "Name", "Brand", "Category")


class FeedError(RuntimeError):
    """A feed could not be parsed or consumed."""


class FeedValidationError(FeedError):
    """A single feed record did not satisfy the input contract."""

    def __init__(self, record_index: int, fields: tuple[str, ...]) -> None:
        self.record_index = record_index
        self.fields = fields
        super().__init__(f"invalid feed record {record_index}: fields={','.join(fields)}")


class ProductEntry(BaseModel):
    """One seller listing, validated without materializing the feed."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    Id: str
    SellerName: str
    Name: str
    Brand: str | None = None
    Category: str | None = None

    @field_validator("Id", "SellerName", "Name")
    @classmethod
    def require_non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("must not be empty")
        return value


@dataclass
class Report:
    """Counters and review details accumulated during one successful import."""

    processed: int = 0
    new: int = 0
    linked: int = 0
    skipped: int = 0
    threat: int = 0
    skipped_entries: list[dict[str, object]] = field(default_factory=list)
    threats: list[dict[str, object]] = field(default_factory=list)


class _PrefixedReader:
    """Expose an already-read probe followed by the original response stream."""

    def __init__(self, prefix: bytes, stream: BinaryIO) -> None:
        self._prefix = prefix
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        if not self._prefix:
            return self._stream.read(size)
        if size == 0:
            return b""
        if size < 0:
            prefix, self._prefix = self._prefix, b""
            return prefix + self._stream.read()
        prefix, self._prefix = self._prefix[:size], self._prefix[size:]
        return prefix


def iter_entries(stream: BinaryIO) -> Iterator[ProductEntry]:
    """Yield validated objects from a JSON array using bounded memory."""
    probe = stream.read(_PROBE_SIZE)
    first = probe.lstrip()[:1]
    if first != b"[":
        raise FeedError("feed root must be a JSON array")

    try:
        raw_entries = ijson.items(_PrefixedReader(probe, stream), "item")
        for index, raw_entry in enumerate(raw_entries):
            if not isinstance(raw_entry, dict):
                raise FeedValidationError(index, ("record",))
            try:
                yield ProductEntry.model_validate(raw_entry)
            except ValidationError as exc:
                fields = tuple(
                    dict.fromkeys(
                        str(error["loc"][0]) for error in exc.errors() if error.get("loc")
                    )
                ) or ("record",)
                raise FeedValidationError(index, fields) from exc
    except FeedValidationError:
        raise
    except Exception as exc:
        raise FeedError(f"invalid JSON feed: {exc}") from exc


def iter_feed(url: str) -> Iterator[ProductEntry]:
    """Stream and parse a remote seller feed without downloading it locally."""
    logger.info("streaming seller feed url=%s", url)
    with requests.get(url, stream=True, timeout=_TIMEOUT) as response:
        response.raise_for_status()
        yield from iter_entries(response.raw)


def screen_entry(entry: ProductEntry, record_index: int, report: Report) -> bool:
    """Reject an entry when libinjection flags one of its string fields."""
    finding: tuple[str, str, str] | None = None
    for field_name in _STRING_FIELDS:
        value = getattr(entry, field_name)
        if value is None:
            continue
        result = libinjection.is_sql_injection(value)
        if result.get("is_sqli") and finding is None:
            finding = (field_name, result.get("fingerprint", ""), value[:120])

    if finding is None:
        return True

    field_name, fingerprint, truncated = finding
    report.threat += 1
    report.threats.append(
        {
            "record_index": record_index,
            "field": field_name,
            "fingerprint": fingerprint,
            "value": truncated,
        }
    )
    logger.warning(
        "event=sqli_attempt record=%d field=%s fingerprint=%s value=%r",
        record_index,
        field_name,
        fingerprint,
        truncated,
    )
    return False
