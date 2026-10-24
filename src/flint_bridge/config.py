import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass(frozen=True)
class Config:
    base_url: str = "http://127.0.0.1:8000"
    ctx: int = 16384
    chunk_tokens: int = 10000
    max_chunks: int = 8
    write_max_tokens: int = 6000
    write_max_files: int = 3
    timeout_s: int = 90
    check_timeout_s: int = 300
    check_default: str = "test"
    check_commands: dict[str, list[str]] = field(default_factory=dict)


def _validate_commands(commands: dict) -> dict[str, list[str]]:
    for name, argv in commands.items():
        ok = isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)
        if not ok:
            raise ValueError(f"check command {name!r} must be a non-empty list of strings")
    return commands


def load_config(root: Path) -> Config:
    """Read root/flint.toml if present, else defaults. FLINT_BASE_URL overrides base_url."""
    cfg = Config()
    path = root / "flint.toml"
    if path.exists():
        data = tomllib.loads(path.read_text())
        server = data.get("server", {})
        budget = data.get("budget", {})
        check = data.get("check", {})
        updates = {
            **{k: server[k] for k in ("base_url", "ctx") if k in server},
            **{
                k: budget[k]
                for k in ("chunk_tokens", "timeout_s", "check_timeout_s")
                if k in budget
            },
        }
        if "default" in check:
            updates["check_default"] = check["default"]
        if "commands" in check:
            updates["check_commands"] = _validate_commands(check["commands"])
        cfg = replace(cfg, **updates)
    env_url = os.environ.get("FLINT_BASE_URL")
    if env_url:
        cfg = replace(cfg, base_url=env_url)
    return cfg
