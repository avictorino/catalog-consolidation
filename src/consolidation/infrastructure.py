"""Infrastructure — adapters to the outside world.

Everything that talks to something external: the HTTP catalog download, the
streamed seller feed and its anti-corruption layer (``ProductEntry``), the
SQL-injection screen, the concrete similarity backends, and the Alembic wiring.
Nothing here holds business rules; it adapts I/O to the shapes the domain and use
cases expect.

Depends on: :mod:`consolidation.domain`, :mod:`consolidation.services`,
:mod:`consolidation.schema`; plus requests / ijson / pydantic / libinjection /
rapidfuzz / alembic.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from difflib import SequenceMatcher
from pathlib import Path
from typing import BinaryIO

import ijson
import libinjection
import requests
from alembic.config import Config as AlembicConfig
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from consolidation.domain import ThreatFinding
from consolidation.services import Similarity

logger = logging.getLogger("consolidation")

_TIMEOUT = (10, 60)  # (connect, read) seconds
_PROBE_SIZE = 64 * 1024
_CHUNK = 1 << 16  # 64 KiB
_SQLITE_MAGIC = b"SQLite format 3\x00"
_STRING_FIELDS = ("Id", "SellerName", "Name", "Brand", "Category")

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


# --------------------------------------------------------------------------- #
# Catalog download
# --------------------------------------------------------------------------- #
def download_to(url: str, dest_dir: Path) -> Path:
    """Stream ``url`` in chunks into a fresh temp file inside ``dest_dir``.

    The response body is never held whole in memory. Returns the temp file path.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / f".catalog-{uuid.uuid4().hex}.db.tmp"
    logger.info("downloading catalog url=%s dest=%s", url, tmp)

    bytes_written = 0
    try:
        with requests.get(url, stream=True, timeout=_TIMEOUT) as response:
            response.raise_for_status()
            with tmp.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=_CHUNK):
                    if chunk:
                        handle.write(chunk)
                        bytes_written += len(chunk)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    logger.info("download complete bytes=%d", bytes_written)
    return tmp


def verify_sqlite_header(path: Path) -> None:
    """Raise ``ValueError`` unless ``path`` starts with the SQLite magic string."""
    with path.open("rb") as handle:
        header = handle.read(len(_SQLITE_MAGIC))
    if header != _SQLITE_MAGIC:
        raise ValueError(f"{path} is not a SQLite database (bad header)")


# --------------------------------------------------------------------------- #
# Alembic wiring — migrations run online with the setup connection injected.
# --------------------------------------------------------------------------- #
def alembic_config(connection) -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", "sqlite://")  # placeholder; connection is injected
    cfg.attributes["connection"] = connection
    return cfg


# --------------------------------------------------------------------------- #
# Anti-corruption layer: the raw seller feed record.
# --------------------------------------------------------------------------- #
class FeedError(RuntimeError):
    """A feed could not be parsed or consumed."""


class FeedValidationError(FeedError):
    """A single feed record did not satisfy the input contract."""

    def __init__(self, record_index: int, fields: tuple[str, ...]) -> None:
        self.record_index = record_index
        self.fields = fields
        super().__init__(f"invalid feed record {record_index}: fields={','.join(fields)}")


class ProductEntry(BaseModel):
    """One seller listing, validated without materializing the feed.

    This is the boundary type. It structurally satisfies ``domain.Submission`` so
    the domain can consume it without importing pydantic.
    """

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


# --------------------------------------------------------------------------- #
# SQL-injection screen (WAF-grade tokenizer). Parameterized SQL is the real
# defense; this rejects and reports probes before they reach the database.
# --------------------------------------------------------------------------- #
def screen_entry(entry: ProductEntry, record_index: int) -> ThreatFinding | None:
    """Return a :class:`ThreatFinding` when libinjection flags a string field."""
    finding: ThreatFinding | None = None
    for field_name in _STRING_FIELDS:
        value = getattr(entry, field_name)
        if value is None:
            continue
        result = libinjection.is_sql_injection(value)
        if result.get("is_sqli"):
            finding = ThreatFinding(field_name, result.get("fingerprint", ""), value[:120])
            break

    if finding is None:
        return None

    logger.warning(
        "event=sqli_attempt record=%d field=%s fingerprint=%s value=%r",
        record_index,
        finding.field,
        finding.fingerprint,
        finding.value,
    )
    return finding


# --------------------------------------------------------------------------- #
# Concrete similarity backends for the ``services.Similarity`` port.
# --------------------------------------------------------------------------- #
class DifflibSimilarity:
    name = "difflib"
    suggested_threshold = 0.90

    def score(self, a: str, b: str) -> float:
        return SequenceMatcher(None, a, b, autojunk=False).ratio()


class RapidFuzzSimilarity:
    name = "rapidfuzz"
    suggested_threshold = 0.90

    def score(self, a: str, b: str) -> float:
        from rapidfuzz import fuzz

        return fuzz.ratio(a, b) / 100.0


def build_similarity(name: str) -> Similarity:
    """Build a backend; rapidfuzz is imported only when that path is used."""
    if name == "difflib":
        return DifflibSimilarity()
    if name == "rapidfuzz":
        return RapidFuzzSimilarity()
    raise ValueError(f"unknown matcher: {name!r} (options: difflib, rapidfuzz)")
