import asyncio
import time
from contextlib import asynccontextmanager
from functools import partial, wraps
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from flint_bridge import ask, check, gitops, lifecycle, llm, read, samples, savings, staging, write
from flint_bridge.budget import parse_path
from flint_bridge.config import Config, load_config
from flint_bridge.log import log_call
from flint_bridge.report import cap, compact, est_tokens, factor_base
from flint_bridge.sysinfo import mem_pressure

INSTRUCTIONS = (
    "You have a local model, Flint (16K context), via these tools. Before doing bulk work "
    "yourself, decide if Flint can do it or a piece of it. Delegate: reading/summarizing big "
    "files or logs, boilerplate, scaffolding, tests from a spec, docstrings, mechanical "
    "refactors, drafts. Keep: planning, architecture, subtle bugs, security-sensitive code, "
    "final review, anything under ~200 output tokens. When you skip Flint, say why in one line. "
    "Size every request to Flint's budget (call flint_status once per session): reads ≤10k "
    "tokens per chunk, ≤8 chunks; writes ≤3 files, ≤6k tokens total. Pass files, "
    "path:start-end ranges or globs, never directories: a read over one chunk is searched "
    "(BM25) down to the blocks that match the question, so use it to find code instead of "
    "grepping, and a big file is written by region. Checks are mandatory: act only on "
    "verified claims and read the cited lines before acting; read every staged patch before "
    "flint_apply; never apply Flint "
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


def tool(description: str):
    """Register fn as a tool whose dict reaches Claude as one block of compact JSON text.

    FastMCP would send it indented, plus a structured copy. fn itself is returned unchanged.
    """

    def register(fn):
        @wraps(fn)
        async def wire(*args, **kwargs) -> str:
            return compact(await fn(*args, **kwargs))

        mcp.add_tool(wire, name=fn.__name__, description=description, structured_output=False)
        return fn

    return register


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
    args: dict | None = None,
    extra_in: int = 0,
    **fields,
) -> dict:
    """Cap and log the report. `args` are what Claude wrote to make the call, and `extra_in` what
    it must read to check the result (cited lines, the patch): together with the report, the
    call's cost to Claude. Delivered work also logs `avoided_in`/`avoided_out` (see savings.py).
    """
    result = cap(result)
    report_tokens = est_tokens(compact(result))
    if outcome is None:
        outcome = _OUTCOME_BY_ERROR.get(result["error"], "error") if "error" in result else "ok"
    try:
        log_call(
            root,
            tool=tool,
            duration_ms=round((time.perf_counter() - started) * 1000),
            report_tokens=report_tokens,
            outcome=outcome,
            cost_in=report_tokens + extra_in,
            cost_out=est_tokens(compact(args or {})),
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


@tool("Health and budgets of the local Flint model. Call once per session before delegating.")
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


@tool(
    "Start/stop/restart the local Flint llama-server using the fixed settings in "
    "~/.flint/server.toml. Usually unnecessary: Flint tools autostart it, and it stops itself "
    "after idle_stop_min to free memory. Use stop when the user needs RAM back, and always "
    "before compiling or other memory-heavy commands."
)
async def flint_server(action: Literal["start", "stop", "restart"]) -> dict:
    started = time.perf_counter()
    report = partial(_report, "flint_server", started=started, args={"action": action})
    cfg = load_config(Path.cwd())
    sc, err = _server_config()
    if err:
        return report(err)
    outcome: dict = {}
    if action in ("stop", "restart"):
        outcome = await asyncio.to_thread(lifecycle.stop, sc)
    if action in ("start", "restart"):
        result = await lifecycle.start(sc, cfg)
        if "error" in result:
            return report(result)
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
    return report(result)


@tool(
    "USE THIS FIRST to read, search or summarize files/logs instead of opening them "
    "yourself. Returns a short answer whose claims are mechanically verified against the source "
    "(unverified claims are removed). Before acting on a claim, read its cited lines yourself. "
    "Pass files, path:start-end ranges or globs (e.g. /repo/src/**/*.py), never directories. "
    "Anything over one chunk is searched: Flint sees only the blocks that best match the "
    "question, and `focus` in the report says how much it saw. Use it to find code instead of "
    "grepping. exhaustive=true reads everything instead: ≤10k tokens of file per chunk, ≤8 "
    "chunks. Not for drafting prose (use flint_ask)."
)
async def flint_read(paths: list[str], question: str, exhaustive: bool = False) -> dict:
    started = time.perf_counter()
    root = _root_of(paths)
    cfg = load_config(root or Path.cwd())
    usage = read.Usage()
    try:
        result = await read.flint_read_impl(
            cfg, paths, question, usage=usage, ready=partial(ensure_up, cfg), focus=not exhaustive
        )
    except llm.FlintOffline:
        result = {"error": "flint_offline"}
    if result.get("error") == "unverifiable_output":
        verdict = "unverifiable"
    else:
        verdict = f"stripped:{result['stripped']}" if result.get("stripped") else "verified"
    _capture(root, cfg, "flint_read", usage, verdict)
    result = cap(result)
    delivered = "answer" in result
    return _report(
        "flint_read",
        factor_base(result),
        started,
        root,
        args={"paths": paths, "question": question, "exhaustive": exhaustive},
        extra_in=savings.verify_tokens(result),  # while the claim paths are still absolute
        avoided_in=usage.seen_tokens if delivered else None,
        stripped=result.get("stripped"),
        flint_tokens_in=usage.tokens_in,
        flint_tokens_out=usage.tokens_out,
        retries=usage.retries,
        reasoning_tokens=usage.reasoning_tokens,
    )


@tool(
    "USE THIS for boilerplate, scaffolding, tests from a spec, docstrings, mechanical "
    "refactors. Flint writes a STAGED patch (your working tree is untouched) and tests run on it "
    "automatically. You MUST read the patch at patch_path before calling flint_apply. To retry, "
    "call again with the same task and your feedback in constraints (max 2 attempts). A file "
    "over ~2k tokens is edited by region: Flint sees an outline and rewrites only the blocks "
    "that match the task, so name the function or class to change. Limits: ≤3 files, ≤6k "
    "tokens of editable text total."
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
        args={"task": task, "files": files, "constraints": constraints},
        extra_in=_patch_tokens(result),
        task_key=write.task_key(task),
        flint_tokens_in=usage.tokens_in,
        flint_tokens_out=usage.tokens_out,
        retries=usage.retries,
        reasoning_tokens=usage.reasoning_tokens,
    )


def _patch_tokens(result: dict) -> int:
    """The staged patch, which Claude must read before applying it."""
    try:
        return est_tokens(Path(result["patch_path"]).read_text(errors="replace"))
    except (KeyError, OSError):
        return 0


def _staged_meta(root: Path, patch_id: str) -> dict:
    return (staging.load_meta(root, patch_id) if root.is_absolute() else None) or {}


def _record_verdict(root: Path, tool: str, patch_id: str, verdict: str) -> None:
    try:
        samples.record(root, load_config(root), tool=tool, verdict=verdict, patch_id=patch_id)
    except (OSError, ValueError):
        pass  # the patch is already applied or rejected: a bad flint.toml must not hide that


@tool(
    "Apply a staged Flint patch to the working tree. Only call after you have read "
    "the patch file yourself and agree with it. Refuses if checks failed or the patch was "
    "modified."
)
async def flint_apply(root: str, patch_id: str) -> dict:
    started = time.perf_counter()
    root_path = Path(root)
    meta = _staged_meta(root_path, patch_id)
    result = await asyncio.to_thread(write.flint_apply_impl, root_path, patch_id)
    outcome = "applied" if result.get("applied") else None
    if outcome:
        _record_verdict(root_path, "flint_apply", patch_id, "applied")
    avoided = (meta.get("avoided") or {}) if outcome else {}
    return _report(
        "flint_apply",
        result,
        started,
        root_path,
        outcome=outcome,
        args={"root": root, "patch_id": patch_id},
        task_key=meta.get("task_key"),
        avoided_in=avoided.get("in"),
        avoided_out=avoided.get("out"),
    )


@tool("Reject a staged patch after review; counts as a failed attempt.")
async def flint_reject(root: str, patch_id: str, reason: str) -> dict:
    started = time.perf_counter()
    root_path = Path(root)
    key = _staged_meta(root_path, patch_id).get("task_key")
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
        args={"root": root, "patch_id": patch_id, "reason": reason},
        task_key=key,
        reason=reason[:200],
    )


@tool(
    "Run an allow-listed check (names from flint.toml, e.g. 'test', 'lint') and get "
    "only the failures. With patch_id, runs against that staged patch in an isolated worktree."
)
async def flint_check(cmd: str, root: str, patch_id: str | None = None) -> dict:
    started = time.perf_counter()
    args = {"cmd": cmd, "root": root, "patch_id": patch_id}
    root_path = Path(root)
    if not root_path.is_absolute():
        return _report("flint_check", {"error": "relative_path", "path": root}, started, args=args)
    cfg = load_config(root_path)
    if patch_id is not None:
        result = await asyncio.to_thread(staging.check_staged_patch, cfg, root_path, patch_id, cmd)
    else:
        result = await asyncio.to_thread(check.run_check, cfg, root_path, cmd)
    outcome = None
    if "passed" in result:
        outcome = "ok" if result["passed"] else "check_failed"
    return _report("flint_check", result, started, root_path, outcome=outcome, args=args)


@tool(
    "Drafts and prose only (docs, emails, commit messages, rewrites, explanations of "
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
        args={"prompt": prompt, "context": context},
        avoided_out=est_tokens(result["draft"]) if "draft" in result else None,
        flint_tokens_in=usage.tokens_in,
        flint_tokens_out=usage.tokens_out,
        retries=usage.retries,
        reasoning_tokens=usage.reasoning_tokens,
    )


def main() -> None:
    mcp.run()
