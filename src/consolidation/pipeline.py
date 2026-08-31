"""End-to-end execution of the consolidation tool."""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine

from consolidation import db_upgrade
from consolidation.feed import FeedValidationError, Report, iter_feed
from consolidation.importer import FeedImporter, load_catalog
from consolidation.similarity import build_similarity
from consolidation.util import download_to, verify_sqlite_header

logger = logging.getLogger("consolidation")

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _alembic_config(connection) -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", "sqlite://")  # placeholder; the connection is injected
    cfg.attributes["connection"] = connection
    return cfg


def _publish(tmp: Path, output: Path) -> None:
    """Atomically replace ``output`` with ``tmp`` (only after a fully successful run)."""
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        logger.warning("replacing existing output path=%s", output)
    tmp.replace(output)
    logger.info("published output path=%s", output)


def _refresh_importer(conn, similarity, threshold: float) -> FeedImporter:
    """Rebuild in-memory indexes after an item transaction is rolled back."""
    catalog = load_catalog(conn)
    importer = FeedImporter(conn, catalog, similarity, threshold)
    conn.commit()
    return importer


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
        logger.error("feed item failures count=%d", report.failed)
        for failure in report.failures:
            logger.error(
                "feed item failure record=%d error=%s",
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
        verify_sqlite_header(tmp)

        engine = create_engine(f"sqlite:///{tmp}")
        try:
            with engine.connect() as conn:
                trans = conn.begin()
                try:
                    source = db_upgrade.classify_source(conn)
                    logger.info("source classified as=%s", source)
                    if source == "unrecognized":
                        raise RuntimeError("unrecognized catalog schema; aborting before any write")

                    command.upgrade(_alembic_config(conn), "head")
                    trans.commit()
                    logger.info("schema refactor committed")

                    similarity = build_similarity(matcher)
                    importer = _refresh_importer(conn, similarity, threshold)
                    report = Report()
                    for record_index, entry in enumerate(iter_feed(products_url)):
                        report.processed += 1
                        if record_index == 0:
                            logger.info("first feed record received")

                        item_report = Report()
                        item_trans = conn.begin()
                        try:
                            importer.process(entry, record_index, item_report)
                            item_trans.commit()
                        except Exception as exc:
                            if item_trans.is_active:
                                item_trans.rollback()
                            report.failed += 1
                            detail = str(exc).splitlines()[0][:200] or type(exc).__name__
                            report.failures.append(
                                {
                                    "record_index": record_index,
                                    "error": f"{type(exc).__name__}: {detail}",
                                }
                            )
                            importer = _refresh_importer(conn, similarity, threshold)
                            continue

                        _merge_item_report(report, item_report)
                        if report.processed % 1000 == 0:
                            logger.info("feed progress processed=%d", report.processed)

                    _log_feed_summary(report)

                    logger.info("feed processing complete")
                except Exception:
                    if trans.is_active:
                        trans.rollback()
                    logger.error("setup or feed stream failed; previous output preserved")
                    raise
        finally:
            engine.dispose()

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
