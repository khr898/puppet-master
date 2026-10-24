# flint-bridge

A stdio MCP server (`flint-engine`) that lets Claude hand bulk reads, writes, checks and drafts to a
local llama-server ("Flint"), with every guardrail enforced in code. The full design is in
[docs/spec/SPEC.md](docs/spec/SPEC.md); the build plan is in [tasks/plan.md](tasks/plan.md).

## Commands

Run every uv command with `UV_CACHE_DIR=.uv-cache`: uv's default cache is blocked by the sandbox.

```bash
UV_CACHE_DIR=.uv-cache uv sync
UV_CACHE_DIR=.uv-cache uv run flint-bridge                  # the MCP server, over stdio
UV_CACHE_DIR=.uv-cache uv run pytest -q
UV_CACHE_DIR=.uv-cache uv run ruff check . && UV_CACHE_DIR=.uv-cache uv run ruff format --check .
UV_CACHE_DIR=.uv-cache uv run flint-stats --since 7d        # summary of the call log
UV_CACHE_DIR=.uv-cache uv run pytest -m live                # needs llama-server on :8000
```

## Flint delegation

This project uses the local `flint-engine` MCP server (Flint, a 16K-context model) to save Claude usage.

**Who does the job:** for each task, decide whether Flint can do it or a piece of it.
- **Delegate:** reading or summarizing large files and logs, boilerplate, scaffolding, tests from a spec, docstrings, mechanical refactors, first drafts.
- **Keep:** planning, architecture, subtle bugs, security-sensitive code, final review, and anything under about 200 tokens of output.
- **When you skip Flint**, say why in one line, for example "skipping Flint: architecture call".

**Mind Flint's context:** run `flint_status()` once per session to get the current budgets.
- `flint_read`: up to ~10k tokens of file content per chunk and 8 chunks per call. Pass specific files or `path:start-end` ranges, never whole directories.
- `flint_write`: up to 3 files, ~6k tokens in total. Split bigger changes into one task per file, and keep any write that touches a file larger than that.
- If a call returns `over_budget`, narrow the request or split it. Don't resend it unchanged.

**Checks are mandatory:**
- **Reads:** act only on claims in a `flint_read` report, since unverified claims have already been removed. Before you edit code or make a decision based on a claim, read its cited lines yourself.
- **Writes:** a `flint_write` produces a staged patch. Read the patch at `patch_path` before calling `flint_apply`. Never apply a patch whose check failed, and never apply a Flint change any other way.
- **Drafts:** treat `flint_ask` output as an unverified draft.
- **Failures:** if a task fails twice, or the tool returns `flint_offline`, do the task yourself and tell the user.

**Free the memory for heavy commands:** Flint holds about 12GB of this Mac's 18GB. Before compiling or running any other memory-heavy command (builds, large test runs, Docker), call `flint_server("stop")`. The next Flint tool call restarts it automatically.

**Check commands for this project** (these must match `flint.toml`):
- test: `uv run pytest -q -x`
- lint: `uv run ruff check .`
