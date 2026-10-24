import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from flint_bridge.check import run_check
from flint_bridge.config import Config

# Dependency folders linked into the worktree instead of copied.
LINKED = (".venv", "node_modules")


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


@contextmanager
def baseline_worktree(root: Path) -> Iterator[Path]:
    """A temporary git worktree whose index and files equal the user's current state.

    That is HEAD plus uncommitted changes plus untracked files, with dependency folders
    linked in. Edits made inside it show up as `git diff`. It is removed on exit.
    """
    tmp = Path(tempfile.mkdtemp(prefix="flint-wt-"))
    try:
        run_git(root, "worktree", "add", "--detach", str(tmp), "HEAD")
        diff = run_git(root, "diff", "HEAD", "--binary")
        if diff:
            run_git(tmp, "apply", "--whitespace=nowarn", input=diff)
        for rel in run_git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
            if not rel or Path(rel).parts[0] in LINKED:
                continue
            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / rel, dest, follow_symlinks=False)
        for name in LINKED:
            if (root / name).exists() and not (tmp / name).exists():
                os.symlink(root / name, tmp / name)
        run_git(tmp, "add", "-A")
        yield tmp
    finally:
        try:
            run_git(root, "worktree", "remove", "--force", str(tmp))
        except GitError:
            pass
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            run_git(root, "worktree", "prune")
        except GitError:
            pass


def _relative_to_root(root: Path, path: Path) -> Path | None:
    """`path` relative to root, or None if it points outside it."""
    resolved_root = root.resolve()
    full = path if path.is_absolute() else resolved_root / path
    # normpath rather than resolve(): the file may not exist yet, and `..` must not escape
    normal = Path(os.path.normpath(full))
    for base in (resolved_root, root):
        try:
            return normal.relative_to(base)
        except ValueError:
            continue
    return None


def stage_and_check(cfg: Config, root: Path, new_files: dict[Path, str], check_name: str) -> dict:
    """Write new_files into a baseline worktree, capture the patch, then run the check there.

    Returns {patch, diffstat, check}, or {error} (not_allowed, path_outside_root, no_changes,
    git_error). The real working tree is never touched.
    """
    if check_name not in cfg.check_commands:
        return {"error": "not_allowed", "allowed": sorted(cfg.check_commands)}
    rels = {}
    for path, content in new_files.items():
        rel = _relative_to_root(root, path)
        if rel is None or not rel.parts:
            return {"error": "path_outside_root", "path": str(path)}
        rels[rel] = content
    try:
        with baseline_worktree(root) as tmp:
            for rel, content in rels.items():
                dest = tmp / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content.encode("utf-8"))
            # intent-to-add makes brand-new files appear in `git diff`
            run_git(tmp, "add", "-N", "--", *(str(r) for r in rels))
            patch = run_git(tmp, "diff", "--binary")
            if not patch:
                return {"error": "no_changes"}
            diffstat = run_git(tmp, "diff", "--stat")
            return {"patch": patch, "diffstat": diffstat, "check": run_check(cfg, tmp, check_name)}
    except GitError as e:
        return {"error": "git_error", "detail": str(e)[:300]}
