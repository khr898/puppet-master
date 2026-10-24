import json
import os
import re
from pathlib import Path

from flint_bridge.check import run_check
from flint_bridge.config import Config
from flint_bridge.gitops import GitError, baseline_worktree, run_git

_ID = re.compile(r"^[0-9a-f]{1,32}$")


def staging_dir(root: Path) -> Path:
    return root / ".flint" / "staging"


def patch_file(root: Path, patch_id: str) -> Path:
    return staging_dir(root) / f"{patch_id}.patch"


def meta_file(root: Path, patch_id: str) -> Path:
    return staging_dir(root) / f"{patch_id}.json"


def valid_id(patch_id: str) -> bool:
    """Patch ids are lowercase hex; anything else could point outside the staging folder."""
    return bool(_ID.match(patch_id))


def check_staged_patch(cfg: Config, root: Path, patch_id: str, name: str) -> dict:
    """Apply a staged patch in a baseline worktree and run the allow-listed check `name`."""
    if not valid_id(patch_id):
        return {"error": "invalid_patch_id"}
    path = patch_file(root, patch_id)
    if not path.is_file():
        return {"error": "unknown_patch"}
    if name not in cfg.check_commands:
        return {"error": "not_allowed", "allowed": sorted(cfg.check_commands)}
    patch = path.read_text(encoding="utf-8")
    try:
        with baseline_worktree(root) as tmp:
            try:
                run_git(tmp, "apply", "--whitespace=nowarn", input=patch)
            except GitError as e:
                return {"error": "does_not_apply", "detail": str(e)[:300]}
            return run_check(cfg, tmp, name)
    except GitError as e:
        return {"error": "git_error", "detail": str(e)[:300]}


def load_meta(root: Path, patch_id: str) -> dict | None:
    """The staged patch's metadata, or None if the id is malformed, missing, or unreadable."""
    if not valid_id(patch_id):
        return None
    try:
        meta = json.loads(meta_file(root, patch_id).read_text())
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def save_meta(root: Path, patch_id: str, meta: dict) -> None:
    path = meta_file(root, patch_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta))
    os.replace(tmp, path)
