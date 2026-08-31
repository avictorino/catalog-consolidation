"""The run report: counters and review details accumulated during one import."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Report:
    """Counters and review details accumulated during one successful import."""

    processed: int = 0
    new: int = 0
    linked: int = 0
    skipped: int = 0
    threat: int = 0
    failed: int = 0
    skipped_entries: list[dict[str, object]] = field(default_factory=list)
    threats: list[dict[str, object]] = field(default_factory=list)
    failures: list[dict[str, object]] = field(default_factory=list)

    def merge_item(self, item: Report) -> None:
        """Fold a single committed entry's counters into the run total."""
        self.new += item.new
        self.linked += item.linked
        self.skipped += item.skipped
        self.threat += item.threat
        self.skipped_entries.extend(item.skipped_entries)
        self.threats.extend(item.threats)
