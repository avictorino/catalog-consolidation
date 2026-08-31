"""Streaming seller-feed acquisition and per-record validation (HTTP + ijson adapter)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import BinaryIO

import ijson
import requests
from pydantic import ValidationError

from consolidation.entries import ProductEntry

logger = logging.getLogger("consolidation")

_TIMEOUT = (10, 60)
_PROBE_SIZE = 64 * 1024


class FeedError(RuntimeError):
    """A feed could not be parsed or consumed."""


class FeedValidationError(FeedError):
    """A single feed record did not satisfy the input contract."""

    def __init__(self, record_index: int, fields: tuple[str, ...]) -> None:
        self.record_index = record_index
        self.fields = fields
        super().__init__(f"invalid feed record {record_index}: fields={','.join(fields)}")


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
