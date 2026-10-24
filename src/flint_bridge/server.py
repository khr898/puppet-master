import json
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from flint_bridge import llm
from flint_bridge.config import load_config
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
    "changes any other way. If Flint is offline or fails twice, do it yourself and tell the user."
)

mcp = FastMCP("flint-engine", instructions=INSTRUCTIONS)


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
    result = {
        "up": h["up"],
        "model": "flint",
        "ctx": h.get("ctx", cfg.ctx),
        "latency_ms": h.get("latency_ms"),
        "mem_pressure": mem_pressure(),
        "budgets": {
            "chunk_tokens": cfg.chunk_tokens,
            "max_chunks": cfg.max_chunks,
            "write_max_tokens": cfg.write_max_tokens,
            "write_max_files": cfg.write_max_files,
            "timeout_s": cfg.timeout_s,
        },
    }
    return _report("flint_status", result, started)


def main() -> None:
    mcp.run()
