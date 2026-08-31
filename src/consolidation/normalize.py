"""Shared string normalization.

One function, applied identically to catalog names, feed names, brand values and
category values (see ``spec/contract.md`` section 3). SQLite ``lower()`` is ASCII-only
and cannot fold accents, so this is done in Python.
"""

from __future__ import annotations

import re
import unicodedata

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
