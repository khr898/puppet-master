import asyncio
import json
import time
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from flint_bridge import ask, check, gitops, lifecycle, llm, read, samples, staging, write
from flint_bridge.budget import parse_path
from flint_bridge.config import Config, load_config
from flint_bridge.log import log_call
from flint_bridge.report import cap, est_tokens
from flint_bridge.sysinfo import mem_pressure

INSTRUCTIONS = (
    "You have a local model, Flint (16K context), via these tools. Before doing bulk work "
    "yourself, decide if Flint can do it or a piece of it. Delegate: reading/summarizing big "
    "files or logs, boilerplate, scaffolding, tests from a spec, docstrings, mechanical "
    "refactors, drafts. Keep: planning, architecture, subtle bugs, security-sensitive code, "
    "final review, anything under ~200 output tokens. When you skip Flint, say why in one line. "
    "Size every request to Flint's budget (call flint_status once per session): reads ≤10k "
    "tokens per chunk, ≤8 chunks; writes ≤3 files, ≤6k tokens total. Pass specific files or "
    "path:start-end ranges. Checks are mandatory: act only on verified claims and read the "
    "cited lines before acting; read every staged patch before flint_apply; never apply Flint "
    "changes any other way. Before compiling or any other memory-heavy command (builds, large "
    'test runs, Docker), call flint_server("stop") to give Flint\'s ~12GB back; the next Flint '
    "call restarts it. If Flint is offline or fails twice, do it yourself and tell the user."
)

idle = lifecycle.IdleWatcher()


def _server_config() -> tuple[lifecycle.ServerConfig | None, dict | None]:
    """(server config, None) or (None, error dict) for a missing or invalid server.toml."""
    try:
        sc = lifecycle.load_server_config()
    except ValueError as e:
        return None, {"error": "bad_server_config", "detail": str(e)}
    if sc is None:
        return None, {"error": "no_server_config", "hint": "see docs/setup/flint-server.md"}
    return sc, None


@asynccontextmanager
async def _lifespan(_server: FastMCP):
    sc, _ = _server_config()
    watcher = asyncio.create_task(idle.run(sc)) if sc else None
    try:
        yield
    finally:
        if watcher:
            watcher.cancel()


mcp = FastMCP("flint-engine", instructions=INSTRUCTIONS, lifespan=_lifespan)


async def ensure_up(cfg: Config) -> dict | None:
    """Autostart Flint if it is down. Returns an error dict for the caller to return, else None.

    Every Flint-calling tool runs this first, so startup time doesn't count toward timeout_s.
    """
    idle.touch()
    if (await llm.health(cfg))["up"]:
        return None
    sc, err = _server_config()
    if err:
        return err
    if not sc.autostart:
        return None
    result = await lifecycle.start(sc, cfg)
    return result if "error" in result else None


_OUTCOME_BY_ERROR = {"flint_offline": "offline", "over_budget": "over_budget"}


def _report(
    tool: str,
    result: dict,
    started: float,
    root: Path | None = None,
    outcome: str | None = None,
    **fields,
) -> dict:
    result = cap(result)
    if outcome is None:
        outcome = _OUTCOME_BY_ERROR.get(result["error"], "error") if "error" in result else "ok"
    try:
        log_call(
            root,
            tool=tool,
            duration_ms=round((time.perf_counter() - started) * 1000),
            report_tokens=est_tokens(json.dumps(result)),
            outcome=outcome,
            **fields,
        )
    except OSError:
        pass  # a log that can't be written must never break a tool
    return result


def _root_of(paths: list[str]) -> Path | None:
    """Project root for config and logs: git toplevel of the first path, else its folder."""
    if not paths:
        return None
    path = parse_path(paths[0])[0]
    if not path.is_absolute():
        return None
    folder = path if path.is_dir() else path.parent
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent  # a glob or missing file: use the nearest folder that exists
    return gitops.toplevel(folder) or folder


def _capture(root: Path | None, cfg: Config, tool: str, usage: read.Usage, verdict: str, **extra):
    """Save every model call of this tool call as a training sample (see samples.py)."""
    if root is None:
        return
    for system, user, raw in usage.calls:
        try:
            samples.record(
                root, cfg, tool=tool, system=system, user=user, raw_output=raw, verdict=verdict,
                **extra,
            )  # fmt: skip
        except OSError:
            pass  # samples are a bonus: never break a tool over them


@mcp.tool(
    description="Health and budgets of the local Flint model. Call once per session before "
    "delegating."
)
async def flint_status() -> dict:
    started = time.perf_counter()
    cfg = load_config(Path.cwd())
    h = await llm.health(cfg)
    sc, _ = _server_config()
    pid = lifecycle.managed_pid(sc) if sc else None
    result = {
        "up": h["up"],
        "model": "flint",
        "ctx": h.get("ctx", cfg.ctx),
        "latency_ms": h.get("latency_ms"),
        "mem_pressure": mem_pressure(),
        "managed": pid is not None,
        "pid": pid,
        "autostart": sc.autostart if sc else None,
        "budgets": {
            "chunk_tokens": cfg.effective_chunk_tokens,
            "max_chunks": cfg.max_chunks,
            "write_max_tokens": cfg.write_max_tokens,
            "write_max_files": cfg.write_max_files,
            "timeout_s": cfg.timeout_s,
        },
    }
    return _report("flint_status", result, started)


@mcp.tool(
    description="Start/stop/restart the local Flint llama-server using the fixed settings in "
    "~/.flint/server.toml. Usually unnecessary: Flint tools autostart it, and it stops itself "
    "after idle_stop_min to free memory. Use stop when the user needs RAM back, and always "
    "before compiling or other memory-heavy commands."
)
async def flint_server(action: Literal["start", "stop", "restart"]) -> dict:
    started = time.perf_counter()
    cfg = load_config(Path.cwd())
    sc, err = _server_config()
    if err:
        return _report("flint_server", err, started)
    outcome: dict = {}
    if action in ("stop", "restart"):
        outcome = await asyncio.to_thread(lifecycle.stop, sc)
    if action in ("start", "restart"):
        result = await lifecycle.start(sc, cfg)
        if "error" in result:
            return _report("flint_server", result, started)
        idle.touch()
    h = await llm.health(cfg)
    pid = lifecycle.managed_pid(sc)
    mem = mem_pressure()
    result = {
        **outcome,
        "up": h["up"],
        "managed": pid is not None,
        "pid": pid,
        "rss_gb": lifecycle.rss_gb(pid) if pid else None,
        "mem_free_pct": None if mem is None else 100 - mem,
    }
    return _report("flint_server", result, started)


@mcp.tool(
    description="USE THIS FIRST to read, search or summarize files/logs instead of opening them "
    "yourself. Returns a short answer whose claims are mechanically verified against the source "
    "(unverified claims are removed). Before acting on a claim, read its cited lines yourself. "
    "Limits: ≤10k tokens of file per chunk, ≤8 chunks; pass specific files or path:start-end "
    "ranges, never directories. Not for drafting prose (use flint_ask)."
)
async def flint_read(paths: list[str], question: str) -> dict:
    started = time.perf_counter()
    root = _root_of(paths)
    cfg = load_config(root or Path.cwd())
    usage = read.Usage()
    try:
        result = await read.flint_read_impl(
            cfg, paths, question, usage=usage, ready=partial(ensure_up, cfg)
        )
    except llm.FlintOffline:
        result = {"error": "flint_offline"}
    if result.get("error") == "unverifiable_output":
        verdict = "unverifiable"
    else:
        verdict = f"stripped:{result['stripped']}" if result.get("stripped") else "verified"
    _capture(root, cfg, "flint_read", usage, verdict)
    return _report(
        "flint_read",
        result,
        started,
        root,
        stripped=result.get("stripped"),
        flint_tokens_in=usage.tokens_in,
        flint_tokens_out=usage.tokens_out,
        retries=usage.retries,
        reasoning_tokens=usage.reasoning_tokens,
    )


@mcp.tool(
    description="USE THIS for boilerplate, scaffolding, tests from a spec, docstrings, mechanical "
    "refactors. Flint writes a STAGED patch (your working tree is untouched) and tests run on it "
    "automatically. You MUST read the patch at patch_path before calling flint_apply. To retry, "
    "call again with the same task and your feedback in constraints (max 2 attempts). Limits: "
    "≤3 files, ≤6k tokens total; larger files stay with you."
)
async def flint_write(task: str, files: list[str], constraints: str = "") -> dict:
    started = time.perf_counter()
    root = _root_of(files)
    cfg = load_config(root or Path.cwd())
    usage = read.Usage()
    try:
        result = await write.flint_write_impl(
            cfg, task, files, constraints, usage=usage, ready=partial(ensure_up, cfg)
        )
    except llm.FlintOffline:
        result = {"error": "flint_offline"}
    outcome = None
    if result.get("give_up"):
        outcome = "miss"
    elif "patch_id" in result:
        outcome = "staged" if result["check"]["passed"] else "check_failed"
    if outcome in ("staged", "check_failed"):
        _capture(root, cfg, "flint_write", usage, outcome, patch_id=result["patch_id"])
    elif result.get("error") == "unverifiable_output":
        _capture(root, cfg, "flint_write", usage, "unverifiable")
    return _report(
        "flint_write",
        result,
        started,
        root,
        outcome=outcome,
        task_key=write.task_key(task),
        flint_tokens_in=usage.tokens_in,
        flint_tokens_out=usage.tokens_out,
        retries=usage.retries,
        reasoning_tokens=usage.reasoning_tokens,
    )


def _staged_task_key(root: Path, patch_id: str) -> str | None:
    meta = staging.load_meta(root, patch_id) if root.is_absolute() else None
    return meta.get("task_key") if meta else None


def _record_verdict(root: Path, tool: str, patch_id: str, verdict: str) -> None:
    try:
        samples.record(root, load_config(root), tool=tool, verdict=verdict, patch_id=patch_id)
    except (OSError, ValueError):
        pass  # the patch is already applied or rejected: a bad flint.toml must not hide that


@mcp.tool(
    description="Apply a staged Flint patch to the working tree. Only call after you have read "
    "the patch file yourself and agree with it. Refuses if checks failed or the patch was "
    "modified."
)
async def flint_apply(root: str, patch_id: str) -> dict:
    started = time.perf_counter()
    root_path = Path(root)
    key = _staged_task_key(root_path, patch_id)
    result = await asyncio.to_thread(write.flint_apply_impl, root_path, patch_id)
    outcome = "applied" if result.get("applied") else None
    if outcome:
        _record_verdict(root_path, "flint_apply", patch_id, "applied")
    return _report("flint_apply", result, started, root_path, outcome=outcome, task_key=key)


@mcp.tool(description="Reject a staged patch after review; counts as a failed attempt.")
async def flint_reject(root: str, patch_id: str, reason: str) -> dict:
    started = time.perf_counter()
    root_path = Path(root)
    key = _staged_task_key(root_path, patch_id)
    result = await asyncio.to_thread(write.flint_reject_impl, root_path, patch_id, reason)
    outcome = "rejected" if result.get("rejected") else None
    if outcome:
        _record_verdict(root_path, "flint_reject", patch_id, "rejected")
    return _report(
        "flint_reject",
        result,
        started,
        root_path,
        outcome=outcome,
        task_key=key,
        reason=reason[:200],
    )


@mcp.tool(
    description="Run an allow-listed check (names from flint.toml, e.g. 'test', 'lint') and get "
    "only the failures. With patch_id, runs against that staged patch in an isolated worktree."
)
async def flint_check(cmd: str, root: str, patch_id: str | None = None) -> dict:
    started = time.perf_counter()
    root_path = Path(root)
    if not root_path.is_absolute():
        return _report("flint_check", {"error": "relative_path", "path": root}, started)
    cfg = load_config(root_path)
    if patch_id is not None:
        result = await asyncio.to_thread(staging.check_staged_patch, cfg, root_path, patch_id, cmd)
    else:
        result = await asyncio.to_thread(check.run_check, cfg, root_path, cmd)
    outcome = None
    if "passed" in result:
        outcome = "ok" if result["passed"] else "check_failed"
    return _report("flint_check", result, started, root_path, outcome=outcome)


@mcp.tool(
    description="Drafts and prose only (docs, emails, commit messages, rewrites, explanations of "
    "text you provide). Output is UNVERIFIED. For any question about code or files, use "
    "flint_read instead."
)
async def flint_ask(prompt: str, context: str = "") -> dict:
    started = time.perf_counter()
    cfg = load_config(Path.cwd())
    usage = read.Usage()
    try:
        result = await ask.flint_ask_impl(
            cfg, prompt, context, usage=usage, ready=partial(ensure_up, cfg)
        )
    except llm.FlintOffline:
        result = {"error": "flint_offline"}
    return _report(
        "flint_ask",
        result,
        started,
        flint_tokens_in=usage.tokens_in,
        flint_tokens_out=usage.tokens_out,
        retries=usage.retries,
        reasoning_tokens=usage.reasoning_tokens,
    )


def main() -> None:
    mcp.run()
