"""Interface layer — the command-line entry point and composition root.

``python -m consolidation.cli`` resolves configuration, picks the concrete
adapters (similarity backend, ``SqliteCatalogRepository``), then wires the use
cases: ``PrepareCatalogDatabaseUseCase`` to get a database ready, then
``ConsolidateCatalogUseCase`` to consume the feed and publish.

Configuration is deliberately small: every option comes from ``.env`` (looked up
next to this package, so it is found from any working directory), and a CLI flag
overrides its ``.env`` value. If an option is set in neither place the run is
invalid — an error is logged and the process exits non-zero.

Depends on: :mod:`consolidation.usecase`, :mod:`consolidation.infrastructure`.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

from consolidation import infrastructure, usecase
from consolidation.services import ProductIdentityResolver

logger = logging.getLogger("consolidation")

# repo root == two levels up from src/consolidation/cli.py
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

MATCHERS = ("difflib", "rapidfuzz")
SOURCES = ("http", "s3")


_LEVEL_COLOR = {
    logging.WARNING: "\033[31m",  # red
    logging.ERROR: "\033[31m",  # red
    logging.CRITICAL: "\033[1;31m",  # bold red
}
_RESET = "\033[0m"


class _ColorFormatter(logging.Formatter):
    def __init__(self, *args: object, color: bool, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._color = color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        prefix = _LEVEL_COLOR.get(record.levelno)
        if self._color and prefix:
            return f"{prefix}{text}{_RESET}"
        return text


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        _ColorFormatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            "%H:%M:%S",
            color=sys.stderr.isatty(),
        )
    )
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
    # Validate in _resolve so invalid CLI values go through the same logged error path
    # as invalid values loaded from .env.
    parser.add_argument("--source", help="byte-stream transport for both URLs: http or s3")
    parser.add_argument("--matcher", help="similarity backend")
    parser.add_argument("--threshold", help="fuzzy cutoff, a float in [0, 1]")
    return parser


class _ConfigError(Exception):
    """A required option is missing or invalid."""


def _resolve(args: argparse.Namespace) -> dict[str, object]:
    env = dotenv_values(ENV_PATH)

    def pick(option: str) -> str:
        attr = option.replace("-", "_")
        value = getattr(args, attr) or env.get(attr.upper())
        if not value:
            raise _ConfigError(f"{option} is not set (pass --{option} or add it to .env)")
        return value

    catalog_url = pick("catalog-url")
    products_url = pick("products-url")
    output = pick("output")
    source = pick("source")
    matcher = pick("matcher")
    threshold_raw = pick("threshold")

    if source not in SOURCES:
        raise _ConfigError(f"source must be one of {SOURCES}, got: {source!r}")

    for name, url in (("catalog-url", catalog_url), ("products-url", products_url)):
        scheme = urlparse(url).scheme
        if source == "s3":
            if scheme not in ("s3", "https"):
                raise _ConfigError(f"{name} must be s3:// or https:// when source=s3, got: {url!r}")
        elif scheme == "http":
            logger.warning("non-TLS URL key=%s url=%s", name, url)
        elif scheme != "https":
            raise _ConfigError(f"{name} must be an http(s) URL, got: {url!r}")

    if matcher not in MATCHERS:
        raise _ConfigError(f"matcher must be one of {MATCHERS}, got: {matcher!r}")

    try:
        threshold = float(threshold_raw)
    except ValueError as exc:
        raise _ConfigError(f"threshold must be a float, got: {threshold_raw!r}") from exc
    if not 0.0 <= threshold <= 1.0:
        raise _ConfigError(f"threshold must be in [0, 1], got: {threshold}")

    return {
        "catalog_url": catalog_url,
        "products_url": products_url,
        "output": Path(output).resolve(),
        "source": source,
        "matcher": matcher,
        "threshold": threshold,
    }


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = _build_parser().parse_args(argv)
    try:
        config = _resolve(args)
    except _ConfigError as exc:
        logger.error("invalid configuration: %s", exc)
        return 2
    logger.info(
        "configuration catalog_url=%s products_url=%s output=%s source=%s matcher=%s threshold=%s",
        config["catalog_url"],
        config["products_url"],
        config["output"],
        config["source"],
        config["matcher"],
        config["threshold"],
    )
    # Composition root: build the collaborators once (the resolver already carries
    # the similarity backend and the threshold), then run the use cases in order.
    resolver = ProductIdentityResolver(
        infrastructure.build_similarity(config["matcher"]), config["threshold"]
    )
    source = infrastructure.build_source(config["source"])
    repository = infrastructure.SqliteCatalogRepository()
    output = config["output"]
    try:
        prepared = usecase.PrepareCatalogDatabaseUseCase(repository, source).execute(
            config["catalog_url"], output.parent
        )
    except Exception:
        logger.exception("run failed")
        return 1
    return usecase.ConsolidateCatalogUseCase(repository, resolver, source).execute(
        prepared, config["products_url"], output
    )


if __name__ == "__main__":
    raise SystemExit(main())
