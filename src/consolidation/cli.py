"""Command-line entry point: ``python -m consolidation.cli``.

This is the composition root: it parses arguments, resolves configuration
(:mod:`consolidation.config`), and hands a :class:`~consolidation.config.RunConfig` to
the :func:`consolidation.consolidate.run` use case.
"""

from __future__ import annotations

import argparse
import logging
import sys

from consolidation import consolidate
from consolidation.config import ConfigError, resolve_config


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    root = logging.getLogger("consolidation")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m consolidation.cli")
    parser.add_argument("--catalog-url", help="HTTP(S) URL of the base SQLite catalog")
    parser.add_argument("--products-url", help="HTTP(S) URL of the seller feed")
    parser.add_argument("--output", help="destination path for the consolidated database")
    # Validation happens in resolve_config so invalid CLI values take the same logged
    # error path as invalid values loaded from .env.
    parser.add_argument("--matcher", help="similarity backend")
    parser.add_argument("--threshold", help="fuzzy cutoff, a float in [0, 1]")
    return parser


logger = logging.getLogger("consolidation")


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = _build_parser().parse_args(argv)
    try:
        config = resolve_config(args)
    except ConfigError as exc:
        logger.error("invalid configuration: %s", exc)
        return 2
    logger.info(
        "configuration catalog_url=%s products_url=%s output=%s matcher=%s threshold=%s",
        config.catalog_url,
        config.products_url,
        config.output,
        config.matcher,
        config.threshold,
    )
    return consolidate.run(config)


if __name__ == "__main__":
    raise SystemExit(main())
