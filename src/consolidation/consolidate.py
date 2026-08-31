"""The consolidation use case: download → refactor → import feed → publish.

Written against the ``CatalogStore`` protocol; ``cli`` wires in the concrete
``CatalogRepository``. A test can pass its own ``store_factory``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from consolidation.config import RunConfig
from consolidation.downloader import download_to
from consolidation.feed_source import FeedValidationError, iter_feed
from consolidation.ports import CatalogStore
from consolidation.report import Report
from consolidation.sqlite_store import CatalogRepository

logger = logging.getLogger("consolidation")


def _publish(tmp: Path, output: Path) -> None:
    """Atomically replace ``output`` with ``tmp`` (only after a fully successful run)."""
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        logger.warning("replacing existing output path=%s", output)
    tmp.replace(output)
    logger.info("published output path=%s", output)


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
    config: RunConfig,
    *,
    store_factory: Callable[[Path], CatalogStore] = CatalogRepository,
) -> int:
    """Execute one consolidation run. Returns a process exit code."""
    logger.info(
        "run config products_url=%s matcher=%s threshold=%s",
        config.products_url,
        config.matcher,
        config.threshold,
    )
    output = Path(config.output).resolve()
    tmp: Path | None = None
    report: Report | None = None
    try:
        tmp = download_to(config.catalog_url, output.parent)

        with store_factory(tmp) as store:
            if store.classify_source() == "unrecognized":
                raise RuntimeError("unrecognized catalog schema; aborting before any write")

            store.migrate()
            store.enable_foreign_keys()
            store.prepare_import(matcher=config.matcher, threshold=config.threshold)

            report = Report()
            for record_index, entry in enumerate(iter_feed(config.products_url)):
                report.processed += 1
                if record_index == 0:
                    logger.info("first feed record received")

                item_report = Report()
                try:
                    with store.item_transaction() as writer:
                        writer.process(entry, record_index, item_report)
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

                report.merge_item(item_report)
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
