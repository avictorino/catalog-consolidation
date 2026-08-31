"""Configuration resolution.

Precedence: ``CLI argument > environment variable > .env file > built-in fallback``
(see ``spec/contract.md`` section 1). ``.env`` is looked up next to the application
entry point, so it is found regardless of the current working directory; its absence
is not an error.
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

logger = logging.getLogger("consolidation")

MATCHERS = ("difflib", "rapidfuzz")

# Built-in fallbacks; identical to the shipped .env.example.
FALLBACKS: dict[str, str] = {
    "CATALOG_URL": "https://engineering-hiring-process.s3.us-east-1.amazonaws.com/catalog.db",
    "PRODUCTS_URL": "https://engineering-hiring-process.s3.us-east-1.amazonaws.com/ProductEntry.json",
    "OUTPUT": "catalog_output.db",
    "MATCHER": "difflib",
    "THRESHOLD": "0.90",
}

# repo root == two levels up from this file (src/consolidation/config.py).
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


@dataclass(frozen=True)
class Config:
    catalog_url: str
    products_url: str
    output: Path
    matcher: str
    threshold: float


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m consolidation.cli",
        description="Consolidate a seller product feed into a marketplace catalog.",
    )
    parser.add_argument("--catalog-url", help="HTTP(S) URL of the base SQLite catalog")
    parser.add_argument("--products-url", help="HTTP(S) URL of the seller feed")
    parser.add_argument("--output", help="destination path for the consolidated database")
    parser.add_argument("--matcher", choices=MATCHERS, help="similarity backend")
    parser.add_argument("--threshold", type=float, help="fuzzy cutoff, a float in [0, 1]")
    return parser


def _resolve(key: str, cli_value: str | None, dotenv: dict[str, str | None]) -> str:
    if cli_value is not None:
        return cli_value
    if os.environ.get(key):
        return os.environ[key]
    if dotenv.get(key):
        return dotenv[key]  # type: ignore[return-value]
    return FALLBACKS[key]


def _require_https(name: str, url: str) -> None:
    scheme = urlparse(url).scheme
    if scheme == "http":
        logger.warning("non-TLS URL accepted key=%s url=%s", name, url)
    elif scheme != "https":
        raise SystemExit(f"{name} must be an http(s) URL, got: {url!r}")


def resolve_config(argv: list[str] | None = None) -> Config:
    args = build_parser().parse_args(argv)
    dotenv = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}

    catalog_url = _resolve("CATALOG_URL", args.catalog_url, dotenv)
    products_url = _resolve("PRODUCTS_URL", args.products_url, dotenv)
    output = _resolve("OUTPUT", args.output, dotenv)
    matcher = _resolve("MATCHER", args.matcher, dotenv)
    threshold_cli = None if args.threshold is None else str(args.threshold)
    threshold_raw = _resolve("THRESHOLD", threshold_cli, dotenv)

    _require_https("catalog-url", catalog_url)
    _require_https("products-url", products_url)

    if matcher not in MATCHERS:
        raise SystemExit(f"unknown matcher: {matcher!r} (options: {', '.join(MATCHERS)})")

    try:
        threshold = float(threshold_raw)
    except ValueError as exc:
        raise SystemExit(f"threshold must be a float, got: {threshold_raw!r}") from exc
    if not 0.0 <= threshold <= 1.0:
        raise SystemExit(f"threshold must be in [0, 1], got: {threshold}")

    return Config(
        catalog_url=catalog_url,
        products_url=products_url,
        output=Path(output),
        matcher=matcher,
        threshold=threshold,
    )
