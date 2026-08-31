"""Leaf helpers with no project dependencies: string normalization and the
chunked catalog download.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from pathlib import Path

import requests

logger = logging.getLogger("consolidation")

# --------------------------------------------------------------------------- #
# Normalization (see spec/contract.md section 3). SQLite lower() is ASCII-only
# and cannot fold accents, so this is done in Python. One function, applied
# identically to catalog names, feed names, brand values and category values.
# --------------------------------------------------------------------------- #
# Quote / apostrophe / prime marks are removed with no replacement, so
# ``Levi's`` -> ``levis`` (matches catalog ``Levis``) and ``12.9''`` -> ``12 9``.
# straight ' ` "  |  curly single/double  |  prime / double-prime  |  acute  |  modifier apostrophe
_QUOTE_MARKS = "'`\"‘’“”′″´ʼ"  # noqa: RUF001 -- enumerating quote/apostrophe glyphs on purpose
_QUOTE_STRIP = {ord(ch): None for ch in _QUOTE_MARKS}
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize(value: str | None) -> str:
    """Return the normalized form of ``value`` (``""`` for ``None``/blank).

    1. Unicode NFKD, drop combining marks (accent folding).
    2. Lowercase.
    3. Strip quote and apostrophe marks (no replacement).
    4. Replace every run of remaining non-alphanumeric characters with a single space.
    5. Collapse whitespace, trim.

    Digits and decimal separators inside numbers become separate tokens: ``12.9"`` /
    ``12.9''`` / ``12.9`` all normalize to ``12 9``.
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    unquoted = without_marks.lower().translate(_QUOTE_STRIP)
    spaced = _NON_ALNUM.sub(" ", unquoted)
    return " ".join(spaced.split())


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
_CHUNK = 1 << 16  # 64 KiB
_SQLITE_MAGIC = b"SQLite format 3\x00"
_TIMEOUT = (10, 60)  # (connect, read) seconds


def download_to(url: str, dest_dir: Path) -> Path:
    """Stream ``url`` in chunks into a fresh temp file inside ``dest_dir``.

    The response body is never held whole in memory (no ``response.content`` /
    ``response.json()``). Returns the path of the downloaded temp file.
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
