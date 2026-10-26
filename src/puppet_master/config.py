import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass(frozen=True)
class Config:
    base_url: str = "http://127.0.0.1:8000"
    # Model id sent with each request: llama-server's --alias, or the name Ollama/LM Studio use.
    model: str = "flint"
    ctx: int = 16384
    # 0 keeps quotes and edits reproducible: the same question gets the same answer.
    temperature: float = 0.0
    chunk_tokens: int = 10000
    max_chunks: int = 8
    write_max_tokens: int = 6000
    write_max_files: int = 3
    timeout_s: int = 90
    check_timeout_s: int = 300
    check_default: str = "test"
    check_commands: dict[str, list[str]] = field(default_factory=dict)
    samples: bool = True
    reasoning_reserve: int = 0
    # File tokens a focused read shows Flint: the blocks that best match the question.
    focus_tokens: int = 3000
    # A file over this many tokens is edited by region: only the parts the task is about.
    region_tokens: int = 2000

    @property
    def effective_chunk_tokens(self) -> int:
        """File tokens per chunk once room is kept free for the model's thinking."""
        return self.chunk_tokens - self.reasoning_reserve


def _validate_commands(commands: dict) -> dict[str, list[str]]:
    for name, argv in commands.items():
        ok = isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)
        if not ok:
            raise ValueError(f"check command {name!r} must be a non-empty list of strings")
    return commands


def _typed(value, kind: type, name: str):
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ValueError(f"{name} must be {kind.__name__}, got {value!r}")
    return value


def load_config(root: Path) -> Config:
    """Read root/puppet-master.toml if present, else defaults.

    PUPPET_BASE_URL and PUPPET_MODEL override base_url and model.
    """
    cfg = Config()
    path = root / "puppet-master.toml"
    if path.exists():
        data = tomllib.loads(path.read_text())
        server = data.get("server", {})
        budget = data.get("budget", {})
        check = data.get("check", {})
        if "temperature" in server:
            t = server["temperature"]
            if isinstance(t, bool) or not isinstance(t, int | float) or not 0 <= t <= 2:
                raise ValueError(f"[server] temperature must be a number from 0 to 2, got {t!r}")
        updates = {
            **{k: server[k] for k in ("base_url", "model", "ctx", "temperature") if k in server},
            **{
                k: budget[k]
                for k in (
                    "chunk_tokens",
                    "timeout_s",
                    "check_timeout_s",
                    "reasoning_reserve",
                    "focus_tokens",
                    "region_tokens",
                )
                if k in budget
            },
        }
        log = _typed(data.get("log", {}), dict, "[log]")
        if "samples" in log:
            updates["samples"] = _typed(log["samples"], bool, "[log] samples")
        if "reasoning_reserve" in budget:
            _typed(budget["reasoning_reserve"], int, "[budget] reasoning_reserve")
        if "default" in check:
            updates["check_default"] = check["default"]
        if "commands" in check:
            updates["check_commands"] = _validate_commands(check["commands"])
        cfg = replace(cfg, **updates)
        if not 0 <= cfg.reasoning_reserve < cfg.chunk_tokens:
            raise ValueError("[budget] reasoning_reserve must be at least 0 and below chunk_tokens")
    env_url = os.environ.get("PUPPET_BASE_URL")
    if env_url:
        cfg = replace(cfg, base_url=env_url.rstrip("/"))
    env_model = os.environ.get("PUPPET_MODEL")
    if env_model:
        cfg = replace(cfg, model=env_model)
    return cfg
