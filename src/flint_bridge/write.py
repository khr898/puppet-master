import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from flint_bridge import attempts, staging
from flint_bridge.budget import HEADROOM, fits
from flint_bridge.config import Config
from flint_bridge.gitops import GitError, run_git, stage_and_check, toplevel
from flint_bridge.read import Usage, _complete_json, _norm
from flint_bridge.report import est_tokens
from flint_bridge.retrieve import Block, rank, split_blocks

NOTES_CHARS = 300
REPORT_FAILURES = 5
REPORT_FAILURE_CHARS = 150
OVER_BUDGET_HINT = "split into one task per file, or name the function to change in the task"
# Largest file edited by region, and the most outline lines shown for the rest of it.
REGION_MAX_FILE_TOKENS = 200_000
# A region block must score at least this share of the best block, and at most this many are
# picked: in a big file most blocks share a word or two with any task, and editable text that
# the task isn't about only invites unasked-for changes.
REGION_MIN_SHARE = 0.5
REGION_MAX_BLOCKS = 4
# Lines that belong to a file's header: imports, includes, package lines, comments.
_HEADER_LINE = re.compile(r"^\s*(import |from \S+ import |#|//|package |use |require|using )")
OUTLINE_LINES = 120

SYSTEM_WRITE = (
    "You edit source files. Return the COMPLETE new content of every file you change — never "
    "diffs or fragments. Do not change files you were not given. A big file comes as an "
    "OUTLINE plus numbered REGIONs: return the COMPLETE new text of each region you change in "
    '"regions"; everything outside the regions stays as it is. Reply with JSON only: '
    '{"files": [{"path": string, "content": string}], '
    '"regions": [{"id": string, "content": string}], "notes": string}.'
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
        "regions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "content": {"type": "string"}},
                "required": ["id", "content"],
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["files", "notes"],
}


def write_schema(whole: bool, regions: bool) -> dict:
    """WRITE_SCHEMA narrowed to the parts this call can use, so the grammar rules out the rest:
    a call whose files are all edited by region can't answer with whole files, and vice versa.
    """
    props = WRITE_SCHEMA["properties"]
    keep = [k for k, on in (("files", whole), ("regions", regions)) if on]
    return {
        "type": "object",
        "properties": {k: props[k] for k in [*keep, "notes"]},
        "required": [*keep, "notes"],
    }


def task_key(task: str) -> str:
    """Stable id for a task, ignoring case and whitespace, so retries are counted together."""
    return hashlib.sha256(_norm(task).lower().encode()).hexdigest()[:12]


def _valid_items(items: object, key: str) -> bool:
    return isinstance(items, list) and all(
        isinstance(f, dict) and isinstance(f.get(key), str) and isinstance(f.get("content"), str)
        for f in items
    )


def _valid_write(data: dict) -> bool:
    return (
        isinstance(data.get("notes"), str)
        and _valid_items(data.get("files", []), "path")
        and _valid_items(data.get("regions", []), "id")
    )


@dataclass(frozen=True)
class Region:
    id: str
    path: Path
    start: int  # first line, 1-based
    end: int  # last line, inclusive


def pick_regions(path: Path, text: str, query: str, budget: int) -> list[Region] | None:
    """The parts of a big file the task is about: its header plus the few top-level blocks that
    clearly match the task best (BM25), up to `budget` tokens, with neighbouring blocks merged.

    None if no block matches the task or the best block alone is over the budget.
    """
    blocks = split_blocks(str(path), 1, text.splitlines())
    if not blocks:
        return None
    size = {id(b): est_tokens("\n".join(b.lines)) for b in blocks}
    scored = rank(query, blocks)
    top = scored[0][0] if scored else 0
    ranked = [b for score, b in scored if score > 0 and score >= REGION_MIN_SHARE * top]
    ranked = ranked[:REGION_MAX_BLOCKS]
    if not ranked or size[id(ranked[0])] > budget:
        return None
    head = [blocks[0]]
    for b in blocks[1:]:
        if not all(_HEADER_LINE.match(x) for x in b.lines if x.strip()):
            break
        head.append(b)
    if sum(size[id(b)] for b in head) > budget // 4:
        head = []
    picked: list[Block] = []
    used = 0
    for b in [*head, *ranked]:
        if b in picked or used + size[id(b)] > budget:
            continue
        picked.append(b)
        used += size[id(b)]
    # A region ends at its last non-blank line, so its text round-trips through the prompt
    # exactly; the blank lines between blocks stay outside every region.
    order = {id(b): i for i, b in enumerate(blocks)}
    spans: list[list[int]] = []
    last = -2
    for b in sorted(picked, key=lambda b: b.start):
        end = b.start + max(i for i, x in enumerate(b.lines) if x.strip() or i == 0)
        if spans and order[id(b)] == last + 1:
            spans[-1][1] = end
        else:
            spans.append([b.start, end])
        last = order[id(b)]
    return [Region("", path, a, z) for a, z in spans]


def _outline(lines: list[str], regions: list[Region]) -> str:
    """Numbered first lines of the top-level blocks outside the regions: read-only context."""
    inside = {n for r in regions for n in range(r.start, r.end + 1)}
    heads = [
        f"{b.start:>5}| {b.lines[0]}"
        for b in split_blocks("", 1, lines)
        if b.start not in inside and b.lines[0].strip()
    ]
    return "\n".join(heads[:OUTLINE_LINES])


def splice(text: str, regions: list[Region], new: dict[str, str]) -> str:
    """text with each region whose id is in `new` replaced by its new content."""
    lines = text.splitlines(keepends=True)
    for r in sorted(regions, key=lambda r: r.start, reverse=True):
        if r.id in new:
            body = new[r.id]
            if body and not body.endswith("\n"):
                body += "\n"
            lines[r.start - 1 : r.end] = [body]
    return "".join(lines)


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


def _write_prompt(
    contents: dict[Path, str | None],
    task: str,
    constraints: str,
    regions: dict[Path, list[Region]] | None = None,
) -> str:
    """Files in sorted order first (stable prefix for the KV cache), then the task and notes.

    A file in `regions` is shown as an outline plus its regions instead of in full.
    """
    regions = regions or {}
    parts = []
    for path, text in sorted(contents.items()):
        if path in regions:
            lines = text.splitlines()
            parts.append(f"=== FILE {path} (OUTLINE; only the REGIONs below can change) ===\n")
            parts.append(f"{_outline(lines, regions[path])}\n")
            for r in regions[path]:
                body = "\n".join(lines[r.start - 1 : r.end])
                parts.append(f"=== REGION {r.id} {path}:{r.start}-{r.end} ===\n{body}\n")
            continue
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
    sizes = {p: est_tokens(t) for p, t in contents.items() if t is not None}
    regions: dict[Path, list[Region]] = {}
    for p, n in sizes.items():
        if n <= cfg.region_tokens or n > REGION_MAX_FILE_TOKENS:
            continue
        picked = pick_regions(p, contents[p], f"{task}\n{constraints}", cfg.region_tokens)
        if picked is None:
            return _over_budget(n, cfg.region_tokens)
        regions[p] = picked
    ids = (f"r{i}" for i in range(1, 1000))
    regions = {p: [replace(r, id=next(ids)) for r in rs] for p, rs in regions.items()}
    editable = {p: n for p, n in sizes.items() if p not in regions}
    for p, rs in regions.items():
        lines = contents[p].splitlines()
        editable[p] = sum(est_tokens("\n".join(lines[r.start - 1 : r.end])) for r in rs)
    if sum(editable.values()) > cfg.write_max_tokens or any(
        n > cfg.write_max_tokens for n in editable.values()
    ):
        return _over_budget(sum(editable.values()), cfg.write_max_tokens)
    prompt = _write_prompt(contents, task, constraints, regions)
    max_out = cfg.write_max_tokens + 500 + cfg.reasoning_reserve
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
        cfg,
        SYSTEM_WRITE,
        prompt,
        write_schema(len(regions) < len(paths), bool(regions)),
        max_out,
        usage,
        _valid_write,
    )
    if data is None:
        return {"error": "unverifiable_output"}
    allowed = {str(p) for p in paths if p not in regions}
    if any(str(Path(f["path"])) not in allowed for f in data.get("files", [])):
        return {"error": "unexpected_path"}
    new_files = {Path(f["path"]): f["content"] for f in data.get("files", [])}
    known = {r.id: p for p, rs in regions.items() for r in rs}
    new_regions = {r["id"]: r["content"] for r in data.get("regions", [])}
    if any(i not in known for i in new_regions):
        return {"error": "unexpected_region"}
    for p, rs in regions.items():
        mine = {r.id: new_regions[r.id] for r in rs if r.id in new_regions}
        if mine:
            new_files[p] = splice(contents[p], rs, mine)
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
    sha256 = hashlib.sha256(patch_bytes).hexdigest()
    staging.meta_file(root, patch_id).write_text(
        json.dumps(
            {
                "patch_id": patch_id,
                "task_key": key,
                "root": str(root),
                "files": staged["files"],
                "sha256": sha256,
                "sig": staging.sign(patch_id, sha256, check["passed"]),
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


def _staged(root: Path, patch_id: str) -> tuple[dict | None, dict | None]:
    """(metadata, None) for a known staged patch, else (None, error dict)."""
    if not root.is_absolute():
        return None, {"error": "relative_path", "path": str(root)}
    if not staging.valid_id(patch_id):
        return None, {"error": "invalid_patch_id"}
    meta = staging.load_meta(root, patch_id)
    if meta is None:
        return None, {"error": "unknown_patch"}
    return meta, None


def flint_apply_impl(root: Path, patch_id: str) -> dict:
    """Apply a staged patch to the working tree, but only if its check passed and it is unchanged.

    This is the only place a Flint change reaches the tree. Every refusal leaves the tree alone.
    """
    meta, err = _staged(root, patch_id)
    if err:
        return err
    if meta.get("applied"):
        return {"error": "already_applied"}
    if not staging.signature_ok(meta, patch_id):
        return {"error": "metadata_modified"}
    if not (meta.get("check") or {}).get("passed"):
        return {"error": "check_failed"}
    try:
        patch_bytes = staging.patch_file(root, patch_id).read_bytes()
    except OSError:
        return {"error": "unknown_patch"}
    if hashlib.sha256(patch_bytes).hexdigest() != meta.get("sha256"):
        return {"error": "patch_modified"}
    # The bytes that were hashed are the bytes handed to git, so the file can't change in between.
    patch = patch_bytes.decode("utf-8")
    try:
        run_git(root, "apply", "--check", "--whitespace=nowarn", input=patch)
        run_git(root, "apply", "--whitespace=nowarn", input=patch)
    except GitError as e:
        return {"error": "does_not_apply", "detail": str(e)[:300]}
    staging.save_meta(root, patch_id, {**meta, "applied": True})
    attempts.reset(root, meta.get("task_key", ""))
    return {"applied": True, "files": meta.get("files", [])}


def flint_reject_impl(root: Path, patch_id: str, reason: str) -> dict:
    """Discard a staged patch after review. A patch that passed its check counts as a failed try.

    One that failed its check was already counted when it was written, so it is not counted twice.
    """
    meta, err = _staged(root, patch_id)
    if err:
        return err
    if meta.get("applied"):
        return {"error": "already_applied"}
    if (meta.get("check") or {}).get("passed"):
        attempts.record(root, meta.get("task_key", ""), passed=False)
    for path in (staging.patch_file(root, patch_id), staging.meta_file(root, patch_id)):
        path.unlink(missing_ok=True)
    return {"rejected": True}
