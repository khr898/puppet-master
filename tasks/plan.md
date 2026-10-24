# Implementation Plan: Usage Saver (Flint delegation MCP bridge)

Spec: [docs/spec/SPEC.md](../docs/spec/SPEC.md) (approved) · Intent: [docs/intent/usage-saver.md](../docs/intent/usage-saver.md) · Tasks: [tasks/todo.md](todo.md)

## Overview

We are building `flint_bridge`, a Python stdio MCP server. It lets Claude (in Claude Desktop's Code tab) hand reads, writes, checks and drafts to a local llama-server ("Flint") while every guardrail is enforced in code:

- **Reads:** claims must quote the source, and the bridge verifies each quote.
- **Writes:** Flint's changes only become staged patches, which run through tests in a git worktree.
- **Apply:** `flint_apply` is the only route into the repo, and it refuses unless the patch passed its check.

Everything except the final live smoke test runs against a **mocked** llama-server, so the whole plan can be built without Flint running.

## Instructions for the builder (Sonnet)

- Work through `tasks/todo.md` **in order**, one task at a time. Each task lists the spec sections to read, so load only those.
- Every interface, prompt, schema and algorithm is already decided in `todo.md`. **Do not redesign.** If something is missing or contradicts the spec, stop and ask the user; don't guess.
- Use TDD: write the listed tests first, watch them fail, then implement.
- After each task, all of these must pass: `uv run pytest -q`, `uv run ruff check .`, and `uv run ruff format --check .`.
- **Sandbox:** uv's default cache (`~/.cache/uv`) is blocked by the sandbox. Run every uv command with `UV_CACHE_DIR=.uv-cache` (for example `UV_CACHE_DIR=.uv-cache uv sync`). `.uv-cache/` is gitignored.
- Commit after each task: `git add -A && git commit -m "<task title>"`.
- Stop at every **Checkpoint** and report to the user before continuing.
- The `.venv/` already exists (Python 3.11.15). Don't recreate it.

## Architecture Decisions

1. **One package, small modules, I/O at the edges.**
   - Pure logic lives in `read.py`, `report.py`, `budget.py` and is unit-tested without mocks.
   - `llm.py` and `gitops.py` are the only modules that talk to the network or to git.
2. **Absolute paths everywhere.**
   - Every tool takes absolute paths.
   - The project root is `git rev-parse --show-toplevel` of the first path, or of the `root` argument for `flint_check` and `flint_apply`.
   - This avoids depending on the MCP server's working directory, which Claude Desktop doesn't guarantee. (This refines the spec: `flint_check` and `flint_apply` gain a `root` argument.)
3. **git generates patches, not difflib.** This refines the spec. In a temporary worktree the bridge:
   - recreates the user's current state (tracked diff plus copied untracked files),
   - runs `git add -A` to set that as the baseline,
   - writes Flint's files,
   - takes `git diff`.

   This handles new files, trailing newlines and binary edge cases correctly.
4. **Flint returns whole files and never diff hunks.** Small models produce broken hunks.
5. **Claims are verified before the merge step.** In multi-chunk reads, the merge step may only pick among already-verified claim ids, so it can't introduce new claims.
6. **State on disk, in the target project:**
   - `.flint/staging/<id>.patch` and `<id>.json`
   - `.flint/attempts.json`
   - `.flint/log.jsonl`
7. **Grammar-constrained JSON.** Requests that need structured output send `response_format` with a `json_schema`, so llama-server can only produce schema-valid JSON. They use `temperature: 0.2`.
8. **The bridge owns llama-server.** It starts and stops it with the fixed settings in `~/.flint/server.toml`:
   - KV cache q8_0, flash attention, `-np 1`, `--cache-reuse`.
   - A memory preflight before every start.
   - Autostart on demand and idle stop after 15 minutes, so the ~12GB is only held while Flint is in use.
9. **Prompts are cache-friendly.** The fixed system prompt comes first, then the files in sorted order, then the question or task last. Follow-ups and retries on the same files reuse the KV cache instead of re-processing about 10k tokens.
10. **No fine-tuning in v1.** It's the last resort, logged in [ADR 0002](../docs/decisions/0002-fine-tuning-last-resort.md) with explicit triggers. Sample capture (T11b) keeps the option open.
11. **Reasoning control.** Flint is a reasoning model: it runs with `--reasoning-budget 0` and thinking is stripped from replies, with a measured fallback (SPEC → Reasoning control). Verified at Checkpoint A.

## Task List (details in todo.md)

### Phase 1: Foundation and round trip
- [x] T1: Project scaffold, config, git init
- [x] T2: Report cap, token estimate, call log
- [x] T3: LLM client (JSON-schema grammar, `cache_prompt`), `flint_status`, server instructions, MCP round trip
- [x] T3b: Server lifecycle: `flint_server`, memory preflight, autostart, idle stop

### Checkpoint A: the bridge is reachable from Claude Desktop (passed 2026-10-24)

### Phase 2: Verified reads (highest-risk guardrail)
- [ ] T4: Line numbering and claim verification, with adversarial fixtures
- [ ] T5: `flint_read` single chunk, with budget preflight and line ranges
- [ ] T6: Multi-chunk map-reduce

### Checkpoint B: reads are safe

### Phase 3: Checks, staged writes, apply
- [ ] T7: Git helpers and `flint_check` (allow-list, failure extraction)
- [ ] T8: Worktree sandbox: baseline, symlinks, run the check
- [ ] T9: `flint_write` (budget, generation, staged patch, auto-check)
- [ ] T10: `flint_apply` and attempt tracking / miss

### Checkpoint C: writes are safe

### Phase 4: Finish
- [ ] T11: `flint_ask` and the `flint-stats` CLI
- [ ] T11b: Sample capture (keeps fine-tuning possible) and the reasoning reserve
- [ ] T12: Final tool descriptions, this project's `CLAUDE.md`, live smoke tests
- [ ] T13: `flint-eval` bake-off harness (Flint UD-IQ2_XXS vs Gemma 4 12B Q4; ADR 0001)

### Checkpoint D: spec success criteria 1–5 met → start the one-week trial

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| The Code tab doesn't load the server from Claude Desktop's MCP config | High | Checkpoint A comes right after T3, before any other work. T3 documents both registration paths. |
| Flint's thinking uses up the 16K window, or fights the JSON grammar | High | `--reasoning-budget 0`, thinking stripped from replies, a reserve-based fallback, and an empirical check at Checkpoint A |
| A smaller model would do this job better | Med | The T13 bake-off against Gemma 4 12B Q4 decides, using the bridge's own tasks |
| Q2 Flint can't produce valid JSON | Low | The JSON-schema grammar makes invalid structure impossible. One retry, then `unverifiable_output`, as a backup. |
| Flint takes up RAM on an 18GB machine | Med | Memory preflight before start, idle stop after 15 minutes, and `flint_server("stop")` on demand. |
| The bridge kills a server it didn't start | Med | The pidfile plus a command-line match. A server it didn't start is reported as `managed: false` and never stopped (T3b tests). |
| The worktree check doesn't match the real tree (untracked files, deps) | Med | Untracked files are copied, `.venv` and `node_modules` are symlinked, and integration tests cover it (T8). |
| Verification passes a real quote that Flint misread | Med | Out of reach of mechanical checks. The `CLAUDE.md` rule makes Claude read the cited lines before acting. |
| The ~6k-token write limit makes `flint_write` rarely usable | Med | Measured in the trial log. Per-function edits are a possible v2. |
| `memory_pressure` output varies by macOS version | Low | Parse failures return `null`. Never fatal. |

## Not in this plan (owned by the user)

- Building llama.cpp, downloading `flint-UD-IQ2_XXS.gguf` (sized for 18GB) and writing `~/.flint/server.toml`, all one-time. Follow [docs/setup/flint-server.md](../docs/setup/flint-server.md). After that the bridge runs the server itself. This is needed only for Checkpoint A's live check and T12's live tests.
- Running the one-week trial and comparing limit hits.
