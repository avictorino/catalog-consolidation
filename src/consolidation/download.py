"""Chunked download of the base catalog and SQLite header verification."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import requests

logger = logging.getLogger("consolidation")

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
