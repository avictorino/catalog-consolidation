from __future__ import annotations

import logging
from pathlib import Path

import pytest

from consolidation import config as config_mod
from consolidation.config import FALLBACKS, resolve_config


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in FALLBACKS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".env"
    monkeypatch.setattr(config_mod, "ENV_PATH", path)
    return path


def test_builtin_fallbacks_when_env_absent(env_file: Path) -> None:
    cfg = resolve_config([])
    assert cfg.catalog_url == FALLBACKS["CATALOG_URL"]
    assert cfg.output == Path("catalog_output.db")
    assert cfg.matcher == "difflib"
    assert cfg.threshold == pytest.approx(0.90)


def test_dotenv_beats_fallback(env_file: Path) -> None:
    env_file.write_text("MATCHER=rapidfuzz\nTHRESHOLD=0.80\n", encoding="utf-8")
    cfg = resolve_config([])
    assert cfg.matcher == "rapidfuzz"
    assert cfg.threshold == pytest.approx(0.80)


def test_env_var_beats_dotenv(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file.write_text("THRESHOLD=0.80\n", encoding="utf-8")
    monkeypatch.setenv("THRESHOLD", "0.70")
    assert resolve_config([]).threshold == pytest.approx(0.70)


def test_cli_beats_everything(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file.write_text("THRESHOLD=0.80\n", encoding="utf-8")
    monkeypatch.setenv("THRESHOLD", "0.70")
    assert resolve_config(["--threshold", "0.5"]).threshold == pytest.approx(0.5)


def test_dotenv_found_from_any_cwd(
    env_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file.write_text("MATCHER=rapidfuzz\n", encoding="utf-8")
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    assert resolve_config([]).matcher == "rapidfuzz"


def test_non_tls_url_warns(env_file: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="consolidation"):
        cfg = resolve_config(["--catalog-url", "http://example.com/catalog.db"])
    assert cfg.catalog_url.startswith("http://")
    assert any("non-TLS" in r.message for r in caplog.records)


@pytest.mark.parametrize("bad", ["1.5", "-0.1", "nope"])
def test_threshold_out_of_range_or_nonfloat(env_file: Path, bad: str) -> None:
    with pytest.raises(SystemExit):
        resolve_config(["--threshold", bad])


def test_unknown_matcher_rejected(env_file: Path) -> None:
    with pytest.raises(SystemExit):
        resolve_config(["--matcher", "fuzzywuzzy"])
