from __future__ import annotations

from consolidation.entries import ProductEntry
from consolidation.report import Report
from consolidation.screening import screen_entry


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


def test_screen_entry_rejects_injection_and_records_truncated_value(caplog) -> None:
    entry = ProductEntry.model_validate(_entry(Brand="TestBrand'; SELECT 1; --" * 10))
    report = Report()
    assert not screen_entry(entry, 4, report)
    assert report.threat == 1
    assert len(report.threats[0]["value"]) == 120
    assert "event=sqli_attempt" in caplog.records[0].message


def test_screen_entry_allows_benign_apostrophes() -> None:
    entry = ProductEntry.model_validate(_entry(Brand="Levi's", Name="iPad Pro 12.9''"))
    assert screen_entry(entry, 0, Report())
