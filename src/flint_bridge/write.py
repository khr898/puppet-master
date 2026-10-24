import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from flint_bridge import attempts, staging
from flint_bridge.budget import HEADROOM, fits
from flint_bridge.config import Config
from flint_bridge.gitops import stage_and_check, toplevel
from flint_bridge.read import Usage, _complete_json, _norm
from flint_bridge.report import est_tokens

NOTES_CHARS = 300
REPORT_FAILURES = 5
REPORT_FAILURE_CHARS = 150
OVER_BUDGET_HINT = "split into one task per file; files over ~6k tokens stay with Claude"

SYSTEM_WRITE = (
    "You edit source files. Return the COMPLETE new content of every file you change — never "
    "diffs or fragments. Do not change files you were not given. Reply with JSON only: "
    '{"files": [{"path": string, "content": string}], "notes": string}.'
)

WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["files", "notes"],
}


def task_key(task: str) -> str:
    """Stable id for a task, ignoring case and whitespace, so retries are counted together."""
    return hashlib.sha256(_norm(task).lower().encode()).hexdigest()[:12]


def _valid_write(data: dict) -> bool:
    files, notes = data.get("files"), data.get("notes")
    return (
        isinstance(notes, str)
        and isinstance(files, list)
        and all(
            isinstance(f, dict)
            and isinstance(f.get("path"), str)
            and isinstance(f.get("content"), str)
            for f in files
        )
    )


def _over_budget(tokens: int, limit: int) -> dict:
    return {"error": "over_budget", "est_tokens": tokens, "limit": limit, "hint": OVER_BUDGET_HINT}


def _repo_root(paths: list[Path]) -> Path | dict:
    """The one git toplevel that holds every path, or an error dict."""
    roots: dict[Path, Path] = {}
    for p in paths:
        folder = p.parent
        while not folder.exists() and folder != folder.parent:
            folder = folder.parent
        top = toplevel(folder)
        if top is None:
            return {"error": "not_in_repo", "path": str(p)}
        roots[top.resolve()] = top
    if len(roots) > 1:
        return {"error": "multiple_repos"}
    return next(iter(roots.values()))


def _write_prompt(contents: dict[Path, str | None], task: str, constraints: str) -> str:
    """Files in sorted order first (stable prefix for the KV cache), then the task and notes."""
    parts = []
    for path, text in sorted(contents.items()):
        body = "(new file)" if text is None else text
        parts.append(f"=== FILE {path} ===\n{body}\n")
    return "".join(parts) + f"\nTASK: {task}\nCONSTRAINTS: {constraints.strip() or 'none'}"


def _short_check(check: dict) -> dict:
    failures = [f[:REPORT_FAILURE_CHARS] for f in check["failures"][:REPORT_FAILURES]]
    return {"passed": check["passed"], "failures": failures}


async def flint_write_impl(
    cfg: Config,
    task: str,
    files: list[str],
    constraints: str,
    *,
    usage: Usage | None = None,
    ready: Callable[[], Awaitable[dict | None]] | None = None,
) -> dict:
    """Have Flint rewrite `files`, stage the result as a patch, and check it in a worktree.

    The working tree is never touched; `flint_apply` is the only way a patch reaches it.
    """
    usage = usage if usage is not None else Usage()
    if not files:
        return {"error": "no_files"}
    for f in files:
        if not Path(f).is_absolute():
            return {"error": "relative_path", "path": f}
    if len(files) > cfg.write_max_files:
        return {
            "error": "over_budget",
            "n_files": len(files),
            "limit": cfg.write_max_files,
            "hint": OVER_BUDGET_HINT,
        }
    paths = sorted({Path(f).resolve() for f in files})
    root = _repo_root(paths)
    if isinstance(root, dict):
        return root

    contents: dict[Path, str | None] = {}
    for p in paths:
        if p.is_dir():
            return {"error": "directory_not_allowed", "path": str(p)}
        try:
            contents[p] = p.read_text(encoding="utf-8") if p.exists() else None
        except (OSError, UnicodeDecodeError):
            return {"error": "unreadable", "path": str(p)}
    sizes = [est_tokens(t) for t in contents.values() if t is not None]
    if sum(sizes) > cfg.write_max_tokens or any(s > cfg.write_max_tokens for s in sizes):
        return _over_budget(sum(sizes), cfg.write_max_tokens)
    prompt = _write_prompt(contents, task, constraints)
    max_out = cfg.write_max_tokens + 500
    prompt_tokens = est_tokens(SYSTEM_WRITE + prompt)
    if not fits(prompt_tokens, max_out, cfg):
        return _over_budget(prompt_tokens, cfg.ctx - HEADROOM - max_out)

    if cfg.check_default not in cfg.check_commands:
        return {
            "error": "no_check_configured",
            "hint": "add [check] default and [check.commands] to the project's flint.toml",
        }
    key = task_key(task)
    if attempts.give_up(root, key):
        return {"give_up": True, "task_key": key}
    if ready is not None and (err := await ready()) is not None:
        return err

    data = await _complete_json(
        cfg, SYSTEM_WRITE, prompt, WRITE_SCHEMA, max_out, usage, _valid_write
    )
    if data is None:
        return {"error": "unverifiable_output"}
    allowed = {str(p) for p in paths}
    if any(str(Path(f["path"])) not in allowed for f in data["files"]):
        return {"error": "unexpected_path"}
    new_files = {Path(f["path"]): f["content"] for f in data["files"]}
    if not new_files:
        return {"error": "no_changes"}

    staged = await asyncio.to_thread(stage_and_check, cfg, root, new_files, cfg.check_default)
    if "error" in staged:
        return staged
    check = staged["check"]

    patch_id = uuid.uuid4().hex[:12]
    patch_bytes = staged["patch"].encode("utf-8")
    patch_path = staging.patch_file(root, patch_id)
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_bytes(patch_bytes)
    staging.meta_file(root, patch_id).write_text(
        json.dumps(
            {
                "patch_id": patch_id,
                "task_key": key,
                "root": str(root),
                "files": staged["files"],
                "sha256": hashlib.sha256(patch_bytes).hexdigest(),
                "check": check,
                "created": datetime.now(UTC).isoformat(),
                "applied": False,
            }
        )
    )
    attempts.record(root, key, passed=check["passed"])
    return {
        "patch_id": patch_id,
        "patch_path": str(patch_path),
        "diffstat": staged["diffstat"].strip(),
        "notes": data["notes"][:NOTES_CHARS],
        "check": _short_check(check),
    }
