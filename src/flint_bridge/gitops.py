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


class GitError(Exception):
    """A git command exited non-zero or could not run."""


def run_git(root: Path, *args: str, input: str | None = None, timeout: int = 60) -> str:
    """Run `git <args>` in root and return stdout. Raises GitError on failure."""
    try:
        run = subprocess.run(
            ["git", *args],
            cwd=root,
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"git {args[0] if args else ''}: {e}") from e
    if run.returncode != 0:
        raise GitError(run.stderr.strip() or run.stdout.strip() or f"git exit {run.returncode}")
    return run.stdout
