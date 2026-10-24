import subprocess
from pathlib import Path


def toplevel(path: Path) -> Path | None:
    """The git toplevel containing path (a directory), or None outside a repository."""
    try:
        run = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = run.stdout.strip()
    return Path(out) if run.returncode == 0 and out else None
