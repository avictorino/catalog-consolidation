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
                    logger.info("schema refactor applied (pending commit)")

                    similarity = build_similarity(matcher)
                    catalog = load_catalog(conn)
                    importer = FeedImporter(conn, catalog, similarity, threshold)
                    report = Report()
                    for record_index, entry in enumerate(iter_feed(products_url)):
                        report.processed += 1
                        if record_index == 0:
                            logger.info("first feed record received")
                        importer.process(entry, record_index, report)
                        if report.processed % 1000 == 0:
                            logger.info("feed progress processed=%d", report.processed)

                    logger.info(
                        "feed summary processed=%d new=%d linked=%d skipped=%d threat=%d",
                        report.processed,
                        report.new,
                        report.linked,
                        report.skipped,
                        report.threat,
                    )
                    if report.threat:
                        logger.warning("feed threats rejected=%d", report.threat)

                    trans.commit()
                    logger.info("commit complete")
                except Exception:
                    trans.rollback()
                    logger.error("transaction rolled back; previous output preserved")
                    raise
        finally:
            engine.dispose()

        _publish(tmp, output)
        tmp = None
        return 0
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
