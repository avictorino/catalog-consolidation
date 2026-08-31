"""SQL-injection screening of feed entries (libinjection adapter, defense in depth)."""

from __future__ import annotations

import logging

import libinjection

from consolidation.entries import STRING_FIELDS, ProductEntry
from consolidation.report import Report

logger = logging.getLogger("consolidation")


def screen_entry(entry: ProductEntry, record_index: int, report: Report) -> bool:
    """Reject an entry when libinjection flags one of its string fields."""
    finding: tuple[str, str, str] | None = None
    for field_name in STRING_FIELDS:
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
