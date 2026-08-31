"""End-to-end execution of the consolidation tool."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from consolidation.feed import FeedValidationError, Report, iter_feed
from consolidation.repository import Catalog, CatalogRepository
from consolidation.resolver import download_to

logger = logging.getLogger("consolidation")


def _publish(tmp: Path, output: Path) -> None:
    """Atomically replace ``output`` with ``tmp`` (only after a fully successful run)."""
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        logger.warning("replacing existing output path=%s", output)
    tmp.replace(output)
    logger.info("published output path=%s", output)


def _merge_item_report(report: Report, item_report: Report) -> None:
    report.new += item_report.new
    report.linked += item_report.linked
    report.skipped += item_report.skipped
    report.threat += item_report.threat
    report.skipped_entries.extend(item_report.skipped_entries)
    report.threats.extend(item_report.threats)


def _log_feed_summary(report: Report) -> None:
    logger.info(
        "feed summary processed=%d new=%d linked=%d skipped=%d threat=%d failed=%d",
        report.processed,
        report.new,
        report.linked,
        report.skipped,
        report.threat,
        report.failed,
    )
    if report.threat:
        logger.warning("feed threats rejected=%d", report.threat)
    if report.failures:
        logger.error("feed item failures count=%d; details follow", report.failed)
        for failure in report.failures:
            logger.error(
                "feed item failure record=%d reason=%s",
                failure["record_index"],
                failure["error"],
            )


def run(
    *,
    catalog_url: str,
    products_url: str,
    output: str | Path,
    matcher: str,
    threshold: float,
    repository_factory: Callable[[Path], Catalog] = CatalogRepository,
) -> int:
    """Execute one consolidation run. Returns a process exit code."""
    logger.info(
        "run config products_url=%s matcher=%s threshold=%s",
        products_url,
        matcher,
        threshold,
    )
    output = Path(output).resolve()
    tmp: Path | None = None
    report: Report | None = None
    try:
        tmp = download_to(catalog_url, output.parent)

        with repository_factory(tmp) as repo:
            if repo.classify_source() == "unrecognized":
                raise RuntimeError("unrecognized catalog schema; aborting before any write")

            repo.migrate()
            repo.enable_foreign_keys()
            repo.prepare_import(matcher=matcher, threshold=threshold)

            report = Report()
            for record_index, entry in enumerate(iter_feed(products_url)):
                report.processed += 1
                if record_index == 0:
                    logger.info("first feed record received")

                item_report = Report()
                try:
                    with repo.item_transaction() as importer:
                        importer.process(entry, record_index, item_report)
                except Exception as exc:
                    report.failed += 1
                    detail = str(exc).splitlines()[0][:200] or type(exc).__name__
                    report.failures.append(
                        {
                            "record_index": record_index,
                            "error": f"{type(exc).__name__}: {detail}",
                        }
                    )
                    continue

                _merge_item_report(report, item_report)
                if report.processed % 1000 == 0:
                    logger.info("feed progress processed=%d", report.processed)

            _log_feed_summary(report)
            logger.info("feed processing complete")

        _publish(tmp, output)
        tmp = None
        return 1 if report and report.failed else 0
    except FeedValidationError as exc:
        logger.error("feed validation failed record=%d fields=%s", exc.record_index, exc.fields)
        logger.exception("run failed")
        return 1
    except Exception:
        logger.exception("run failed")
        return 1
    finally:
        if tmp is not None:
            Path(tmp).unlink(missing_ok=True)
