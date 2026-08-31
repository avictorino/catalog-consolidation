from __future__ import annotations

import logging
from pathlib import Path

import pytest

from consolidation import cli, config
from consolidation.config import RunConfig

ENV_KEYS = ("CATALOG_URL", "PRODUCTS_URL", "OUTPUT", "MATCHER", "THRESHOLD")
FULL_ENV = {
    "CATALOG_URL": "https://example.com/catalog.db",
    "PRODUCTS_URL": "https://example.com/ProductEntry.json",
    "OUTPUT": "catalog_output.db",
    "MATCHER": "rapidfuzz",
    "THRESHOLD": "0.90",
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
    monkeypatch.setattr(config, "ENV_PATH", path)
    return path


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")


def _resolve(argv: list[str]) -> RunConfig:
    return config.resolve_config(cli._build_parser().parse_args(argv))


def test_resolves_from_env_file(env_file: Path) -> None:
    _write_env(env_file, FULL_ENV)
    assert _resolve([]) == RunConfig(
        catalog_url="https://example.com/catalog.db",
        products_url="https://example.com/ProductEntry.json",
        output="catalog_output.db",
        matcher="rapidfuzz",
        threshold=pytest.approx(0.90),
    )


def test_cli_flag_overrides_env(env_file: Path) -> None:
    _write_env(env_file, FULL_ENV)
    resolved = _resolve(["--matcher", "rapidfuzz", "--threshold", "0.5"])
    assert resolved.matcher == "rapidfuzz"
    assert resolved.threshold == pytest.approx(0.5)


@pytest.mark.parametrize("missing", ENV_KEYS)
def test_missing_key_invalidates_run(env_file: Path, missing: str) -> None:
    _write_env(env_file, {k: v for k, v in FULL_ENV.items() if k != missing})
    with pytest.raises(config.ConfigError):
        _resolve([])


def test_missing_env_file_invalidates_run(env_file: Path) -> None:
    assert not env_file.exists()
    with pytest.raises(config.ConfigError):
        _resolve([])


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
    assert _resolve([]).matcher == "rapidfuzz"


def test_non_tls_url_warns(env_file: Path, caplog: pytest.LogCaptureFixture) -> None:
    _write_env(env_file, {**FULL_ENV, "CATALOG_URL": "http://example.com/catalog.db"})
    with caplog.at_level(logging.WARNING, logger="consolidation"):
        resolved = _resolve([])
    assert resolved.catalog_url.startswith("http://")
    assert any("non-TLS" in r.message for r in caplog.records)


@pytest.mark.parametrize("bad", ["1.5", "-0.1", "nope"])
def test_bad_threshold_rejected(env_file: Path, bad: str) -> None:
    _write_env(env_file, {**FULL_ENV, "THRESHOLD": bad})
    with pytest.raises(config.ConfigError):
        _resolve([])


def test_unknown_matcher_in_env_rejected(env_file: Path) -> None:
    _write_env(env_file, {**FULL_ENV, "MATCHER": "fuzzywuzzy"})
    with pytest.raises(config.ConfigError):
        _resolve([])
