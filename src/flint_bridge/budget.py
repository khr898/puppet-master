import glob
import re
from pathlib import Path

from flint_bridge.config import Config

_RANGE = re.compile(r"^(.*):(\d+)-(\d+)$")

# Tokens kept free in the window beyond the prompt and the answer.
HEADROOM = 512


class PathError(Exception):
    """A request path is unusable. `result` is the error report to hand back to Claude."""

    def __init__(self, result: dict):
        super().__init__(result.get("error"))
        self.result = result


def parse_path(p: str) -> tuple[Path, int | None, int | None]:
    """Split `path` or `path:START-END` into (path, start, end)."""
    m = _RANGE.match(p)
    if m is None:
        return Path(p), None, None
    return Path(m.group(1)), int(m.group(2)), int(m.group(3))


def expand(paths: list[str]) -> list[tuple[Path, int | None, int | None]]:
    """Expand globs. Raises PathError for relative paths and directories."""
    out = []
    for p in paths:
        path, start, end = parse_path(p)
        if not path.is_absolute():
            raise PathError({"error": "relative_path", "path": p})
        # A path with no match is kept as-is so the caller can report it as unreadable.
        for match in sorted(glob.glob(str(path), recursive=True)) or [str(path)]:
            if Path(match).is_dir():
                raise PathError({"error": "directory_not_allowed", "path": match})
            out.append((Path(match), start, end))
    return out


def fits(prompt_tokens: int, max_out: int, cfg: Config) -> bool:
    return prompt_tokens + max_out <= cfg.ctx - HEADROOM
