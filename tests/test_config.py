from pathlib import Path

import pytest

from flint_bridge.config import Config, load_config

EXAMPLE = """
[check]
default = "test"
[check.commands]
test = ["uv", "run", "pytest", "-q", "-x"]
lint = ["uv", "run", "ruff", "check", "."]
[budget]
chunk_tokens = 10000
timeout_s = 90
"""


def test_defaults_when_file_missing(tmp_path: Path):
    assert load_config(tmp_path) == Config()


def test_parses_spec_example(tmp_path: Path):
    (tmp_path / "flint.toml").write_text(EXAMPLE)
    cfg = load_config(tmp_path)
    assert cfg.check_default == "test"
    assert cfg.check_commands["test"] == ["uv", "run", "pytest", "-q", "-x"]
    assert cfg.check_commands["lint"] == ["uv", "run", "ruff", "check", "."]
    assert cfg.chunk_tokens == 10000
    assert cfg.timeout_s == 90


def test_server_and_budget_keys_override_defaults(tmp_path: Path):
    (tmp_path / "flint.toml").write_text(
        '[server]\nbase_url = "http://127.0.0.1:9000"\nctx = 8192\n'
        "[budget]\nchunk_tokens = 5000\ncheck_timeout_s = 60\n"
    )
    cfg = load_config(tmp_path)
    assert cfg.base_url == "http://127.0.0.1:9000"
    assert cfg.ctx == 8192
    assert cfg.chunk_tokens == 5000
    assert cfg.check_timeout_s == 60


def test_env_overrides_base_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "flint.toml").write_text('[server]\nbase_url = "http://127.0.0.1:9000"\n')
    monkeypatch.setenv("FLINT_BASE_URL", "http://127.0.0.1:7777")
    assert load_config(tmp_path).base_url == "http://127.0.0.1:7777"


def test_rejects_string_command(tmp_path: Path):
    (tmp_path / "flint.toml").write_text('[check.commands]\ntest = "pytest"\n')
    with pytest.raises(ValueError):
        load_config(tmp_path)


def test_rejects_empty_command(tmp_path: Path):
    (tmp_path / "flint.toml").write_text("[check.commands]\ntest = []\n")
    with pytest.raises(ValueError):
        load_config(tmp_path)


def test_rejects_non_string_items(tmp_path: Path):
    (tmp_path / "flint.toml").write_text('[check.commands]\ntest = ["pytest", 1]\n')
    with pytest.raises(ValueError):
        load_config(tmp_path)


def test_sample_capture_and_reasoning_reserve_default_to_on_and_zero(tmp_path: Path):
    cfg = load_config(tmp_path)
    assert cfg.samples is True
    assert cfg.reasoning_reserve == 0
    assert cfg.effective_chunk_tokens == cfg.chunk_tokens


def test_parses_samples_and_reasoning_reserve(tmp_path: Path):
    (tmp_path / "flint.toml").write_text(
        "[log]\nsamples = false\n[budget]\nreasoning_reserve = 3000\n"
    )
    cfg = load_config(tmp_path)
    assert cfg.samples is False
    assert cfg.reasoning_reserve == 3000
    assert cfg.effective_chunk_tokens == 7000
