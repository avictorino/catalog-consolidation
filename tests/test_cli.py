from __future__ import annotations

import logging
from pathlib import Path

import pytest

from consolidation import cli

ENV_KEYS = ("CATALOG_URL", "PRODUCTS_URL", "OUTPUT", "SOURCE", "MATCHER")
FULL_ENV = {
    "CATALOG_URL": "https://example.com/catalog.db",
    "PRODUCTS_URL": "https://example.com/ProductEntry.json",
    "OUTPUT": "catalog_output.db",
    "SOURCE": "http",
    "MATCHER": "rapidfuzz",
}


@pytest.fixture(autouse=True)
def _isolate_logger() -> object:
    """Keep the package logger propagating to root so ``caplog`` can see records,
    and restore whatever state each test (or ``cli.main``) leaves behind.
    """
    lg = logging.getLogger("consolidation")
    saved = (lg.handlers[:], lg.propagate, lg.level)
    lg.handlers.clear()
    lg.propagate = True
    lg.setLevel(logging.INFO)
    yield
    lg.handlers[:], lg.propagate, level = saved
    lg.setLevel(level)


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".env"
    monkeypatch.setattr(cli, "ENV_PATH", path)
    return path


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")


def test_resolves_from_env_file(env_file: Path) -> None:
    _write_env(env_file, FULL_ENV)
    args = cli._build_parser().parse_args([])
    config = cli._resolve(args)
    assert config == {
        "catalog_url": "https://example.com/catalog.db",
        "products_url": "https://example.com/ProductEntry.json",
        "output": Path("catalog_output.db").resolve(),
        "source": "http",
        "matcher": "rapidfuzz",
    }


def test_cli_flag_overrides_env(env_file: Path) -> None:
    _write_env(env_file, {**FULL_ENV, "MATCHER": "difflib"})
    args = cli._build_parser().parse_args(["--matcher", "rapidfuzz"])
    config = cli._resolve(args)
    assert config["matcher"] == "rapidfuzz"


@pytest.mark.parametrize("missing", ENV_KEYS)
def test_missing_key_invalidates_run(env_file: Path, missing: str) -> None:
    _write_env(env_file, {k: v for k, v in FULL_ENV.items() if k != missing})
    args = cli._build_parser().parse_args([])
    with pytest.raises(cli._ConfigError):
        cli._resolve(args)


def test_missing_env_file_invalidates_run(env_file: Path) -> None:
    assert not env_file.exists()
    args = cli._build_parser().parse_args([])
    with pytest.raises(cli._ConfigError):
        cli._resolve(args)


def test_main_returns_2_and_logs_error_on_bad_config(
    env_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main([])
    assert rc == 2
    assert "invalid configuration" in capsys.readouterr().err


def test_env_found_from_any_cwd(
    env_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_env(env_file, FULL_ENV)
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    assert cli._resolve(cli._build_parser().parse_args([]))["matcher"] == "rapidfuzz"


def test_non_tls_url_warns(env_file: Path, caplog: pytest.LogCaptureFixture) -> None:
    _write_env(env_file, {**FULL_ENV, "CATALOG_URL": "http://example.com/catalog.db"})
    with caplog.at_level(logging.WARNING, logger="consolidation"):
        config = cli._resolve(cli._build_parser().parse_args([]))
    assert config["catalog_url"].startswith("http://")
    assert any("non-TLS" in r.message for r in caplog.records)


def test_unknown_matcher_in_env_rejected(env_file: Path) -> None:
    _write_env(env_file, {**FULL_ENV, "MATCHER": "fuzzywuzzy"})
    with pytest.raises(cli._ConfigError):
        cli._resolve(cli._build_parser().parse_args([]))


def test_unknown_source_rejected(env_file: Path) -> None:
    _write_env(env_file, {**FULL_ENV, "SOURCE": "ftp"})
    with pytest.raises(cli._ConfigError, match="source must be one of"):
        cli._resolve(cli._build_parser().parse_args([]))


def test_s3_source_only_affects_the_feed_url(env_file: Path) -> None:
    _write_env(
        env_file,
        {
            **FULL_ENV,
            "SOURCE": "s3",
            "CATALOG_URL": "https://example.com/catalog.db",  # still plain HTTP(S)
            "PRODUCTS_URL": "s3://bucket/ProductEntry.json",
        },
    )
    config = cli._resolve(cli._build_parser().parse_args([]))
    assert config["source"] == "s3"
    assert config["catalog_url"] == "https://example.com/catalog.db"
    assert config["products_url"] == "s3://bucket/ProductEntry.json"


def test_s3_source_rewrites_amazonaws_feed_url_to_s3_scheme(env_file: Path) -> None:
    _write_env(
        env_file,
        {
            **FULL_ENV,
            "SOURCE": "s3",
            "PRODUCTS_URL": "https://s3.us-east-1.amazonaws.com/engineering-hiring-process/ProductEntry.json",
        },
    )
    config = cli._resolve(cli._build_parser().parse_args([]))
    assert config["catalog_url"] == "https://example.com/catalog.db"  # untouched
    assert config["products_url"] == "s3://engineering-hiring-process/ProductEntry.json"


def test_s3_source_rejects_non_s3_feed_url(env_file: Path) -> None:
    _write_env(
        env_file,
        {**FULL_ENV, "SOURCE": "s3", "PRODUCTS_URL": "http://bucket/ProductEntry.json"},
    )
    with pytest.raises(cli._ConfigError, match="s3:// or amazonaws.com URL"):
        cli._resolve(cli._build_parser().parse_args([]))


def test_s3_source_still_validates_catalog_url_as_http(env_file: Path) -> None:
    _write_env(
        env_file,
        {**FULL_ENV, "SOURCE": "s3", "CATALOG_URL": "s3://bucket/catalog.db"},
    )
    with pytest.raises(cli._ConfigError, match="catalog-url must be an http"):
        cli._resolve(cli._build_parser().parse_args([]))
