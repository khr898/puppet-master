import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from flint_bridge import lifecycle, llm
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


def _report(tool: str, result: dict, started: float, root: Path | None = None) -> dict:
    result = cap(result)
    try:
        log_call(
            root,
            tool=tool,
            duration_ms=round((time.perf_counter() - started) * 1000),
            report_tokens=est_tokens(json.dumps(result)),
            outcome="error" if "error" in result else "ok",
        )
    except OSError:
        pass  # a log that can't be written must never break a tool
    return result


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
            "chunk_tokens": cfg.chunk_tokens,
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


def main() -> None:
    mcp.run()
