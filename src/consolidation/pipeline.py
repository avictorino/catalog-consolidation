"""End-to-end execution of the consolidation tool.

One run: download the base catalog, classify it, and — inside a single transaction —
apply the schema refactor (Alembic revision ``0001``). Feed import is PR #3; for now
the pipeline stops after the refactor and publishes the refactored catalog.

Any failure rolls the transaction back, discards the temp file, and leaves the
previous output untouched.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine

from consolidation import database
from consolidation.config import Config
from consolidation.download import download_to, verify_sqlite_header

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


def run(config: Config) -> int:
    """Execute one consolidation run. Returns a process exit code."""
    output = config.output.resolve()
    tmp: Path | None = None
    try:
        tmp = download_to(config.catalog_url, output.parent)
        verify_sqlite_header(tmp)

        engine = create_engine(f"sqlite:///{tmp}")
        try:
            with engine.connect() as conn:
                trans = conn.begin()
                try:
                    source = database.classify_source(conn)
                    logger.info("source classified as=%s", source)
                    if source == "unrecognized":
                        raise RuntimeError("unrecognized catalog schema; aborting before any write")

                    command.upgrade(_alembic_config(conn), "head")
                    logger.info("schema refactor applied (pending commit)")

                    # TODO(PR #3): stream ProductEntry.json and consolidate the feed here,
                    # inside this same transaction.

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
    except Exception:
        logger.exception("run failed")
        return 1
    finally:
        if tmp is not None:
            Path(tmp).unlink(missing_ok=True)
