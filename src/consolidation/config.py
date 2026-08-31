"""Run configuration: resolution from ``.env`` + CLI flags, and validation.

Every option comes from ``.env`` (looked up next to this package, so it is found from
any working directory); a CLI flag overrides its ``.env`` value. A missing or invalid
option makes the run invalid before any work starts.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

logger = logging.getLogger("consolidation")

# repo root == two levels up from src/consolidation/config.py
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

MATCHERS = ("difflib", "rapidfuzz")


class ConfigError(Exception):
    """A required option is missing or invalid."""


@dataclass(frozen=True)
class RunConfig:
    catalog_url: str
    products_url: str
    output: str
    matcher: str
    threshold: float


def resolve_config(args: argparse.Namespace) -> RunConfig:
    env = dotenv_values(ENV_PATH)

    def pick(option: str) -> str:
        attr = option.replace("-", "_")
        value = getattr(args, attr, None) or env.get(attr.upper())
        if not value:
            raise ConfigError(f"{option} is not set (pass --{option} or add it to .env)")
        return value

    catalog_url = pick("catalog-url")
    products_url = pick("products-url")
    output = pick("output")
    matcher = pick("matcher")
    threshold_raw = pick("threshold")

    for name, url in (("catalog-url", catalog_url), ("products-url", products_url)):
        scheme = urlparse(url).scheme
        if scheme == "http":
            logger.warning("non-TLS URL key=%s url=%s", name, url)
        elif scheme != "https":
            raise ConfigError(f"{name} must be an http(s) URL, got: {url!r}")

    if matcher not in MATCHERS:
        raise ConfigError(f"matcher must be one of {MATCHERS}, got: {matcher!r}")

    try:
        threshold = float(threshold_raw)
    except ValueError as exc:
        raise ConfigError(f"threshold must be a float, got: {threshold_raw!r}") from exc
    if not 0.0 <= threshold <= 1.0:
        raise ConfigError(f"threshold must be in [0, 1], got: {threshold}")

    return RunConfig(
        catalog_url=catalog_url,
        products_url=products_url,
        output=output,
        matcher=matcher,
        threshold=threshold,
    )
