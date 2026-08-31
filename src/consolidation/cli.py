"""Command-line entry point: ``python -m consolidation.cli``.

Resolves configuration, configures logging, and hands off to :mod:`consolidation.pipeline`.
The same module is invoked on every run; it decides whether the downloaded catalog is
the legacy model and applies the refactor if so.
"""

from __future__ import annotations

import logging
import sys

from consolidation import pipeline
from consolidation.config import resolve_config

logger = logging.getLogger("consolidation")


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    root = logging.getLogger("consolidation")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    config = resolve_config(argv)
    logger.info(
        "configuration catalog_url=%s products_url=%s output=%s matcher=%s threshold=%s",
        config.catalog_url,
        config.products_url,
        config.output,
        config.matcher,
        config.threshold,
    )
    return pipeline.run(config)


if __name__ == "__main__":
    raise SystemExit(main())
