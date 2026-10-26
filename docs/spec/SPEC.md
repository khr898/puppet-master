# Spec: Usage Saver — Flint delegation MCP bridge

Status: **Approved 2026-10-24** · Intent: [../intent/usage-saver.md](../intent/usage-saver.md)

## Assumptions

1. The bridge is **one Python MCP server** (stdio). It is specced as a single unit, not a capability map, because every tool shares one process, one llama-server client, one config, and one log.
2. Target projects that Flint writes to are **git repositories**. Staging and checks depend on `git worktree` and `git apply`.
3. llama-server exposes the OpenAI-compatible `/v1/chat/completions` on `127.0.0.1:8000`. The user builds llama.cpp and downloads the model once. After that, **the bridge launches and stops llama-server itself** with fixed settings from a config file (see Server lifecycle).
4. "Claude reads every diff" means Claude opens the staged patch file with its own Read tool. The ≤500-token report never contains the diff itself.
5. Reports are counted as `ceil(chars / 4)` tokens. That's an estimate, not a tokenizer.

## Objective

Cut Claude Pro usage in Claude Desktop's Code tab by letting Claude hand bulk exploring and writing to a local model (Flint), **without any Flint mistake reaching the project**.

- **User:** solo developer, Claude Pro, M3 Pro with 18GB.
- **Who decides:** Claude decides case by case what to delegate. There is no routing table or classifier.
- **Guardrails:** checks are **mandatory and enforced by the bridge in code**. They are never left to Claude's discretion.

### User stories

- As Claude, I can ask Flint a question about large files or logs and get back a short report where every claim has been mechanically verified against the source.
- As Claude, I can have Flint draft a code change that lands only as a staged patch, and the patch can't be applied until checks pass and I approve it.
- As Claude, I can run allow-listed tests and lint and get back only the failures.
- As the user, I can read a call log that shows delegation rate, accept rate, report sizes, and misses.

## Tech Stack

- Python ≥ 3.11, managed with `uv`
- `mcp` (official Python SDK, `mcp.server.fastmcp.FastMCP`), using stdio transport
- `httpx` for llama-server calls
- `pytest`, `pytest-asyncio`, `respx` (mocks httpx), and `ruff`
- Standard library: `subprocess` (no shell; git produces the patches), `tomllib`, `hashlib`, `json`

## Tools (the contract)

Every tool returns a JSON report capped at **500 tokens**. A report that would go over the cap is truncated with `"truncated": true`. Raw file contents and full Flint output never go back to Claude.

| Tool | Input | Returns |
|---|---|---|
| `puppet_status()` | — | `{up, model, ctx, latency_ms, mem_pressure}` (memory pressure comes from `memory_pressure -Q` / `vm_stat`) |
| `puppet_read(paths, question)` | file/glob paths, a question | `{answer, claims[], stripped, chunks}` (see Read verification) |
| `puppet_write(task, files, constraints)` | a task, target files, constraints | `{patch_id, patch_path, diffstat, notes, check}` (see Write pipeline) |
| `puppet_check(cmd, root, patch_id?)` | an allow-listed command name, the repo root, plus an optional staged patch | `{passed, failures[] (≤20), exit_code}` |
| `puppet_apply(root, patch_id)` | repo root, patch id | `{applied, files}`. **Refuses** unless the patch's check passed and its hash hasn't changed. |
| `puppet_reject(root, patch_id, reason)` | repo root, patch id, reason | Deletes the staged patch and counts it as a failed attempt |
| `puppet_ask(prompt, context)` | prompt, plus optional text context | `{draft, verified: false}`. Always marked unverified. |
| `puppet_server(action)` | `start` \| `stop` \| `restart` | `{up, managed, pid, rss_gb, mem_free_pct}` (see Server lifecycle) |

All paths are absolute. The project root is the git toplevel of the paths given.

### Read verification (mandatory)

1. The bridge reads the files itself and prefixes each line with its number (`  42| code`).
2. Files are split into chunks that fit the context: the 16384-token window minus room for the prompt and the answer, with a default budget of 10k tokens of file per call. When there are several chunks, they are processed map-reduce style: per-chunk answers first, then a merge step.
   - **Focused reads (the default):** when the files need more than one chunk, the bridge searches them instead (`retrieve.py`). It splits each file into top-level blocks (a new block starts at an unindented line after a blank line, at most 60 lines), scores every block against the question with BM25 over identifier terms (`parseHTTPResponse` → `parsehttpresponse`, `parse`, `http`, `response`), doubles the score of a block that defines a name the question mentions, drops blocks under 20% of the best score, and packs the rest, best first, into one chunk of `[budget].focus_tokens` (default 3,000). Each of the top three blocks brings along the blocks in its file that define the names it uses, so a function arrives with its constants and helpers. Lines left out are `None`: they render as one `...| (lines not shown)` marker per gap, and a claim citing one fails verification. The report carries `focus: {blocks, of_blocks, shown_tokens, total_tokens}`. With no block sharing a term with the question, the normal chunk limits apply. `exhaustive=true` skips the search.
3. Flint must return JSON: `{"answer": str, "claims": [{"text", "file", "line", "quote"}]}`. This is **grammar-constrained**: the request sends `response_format: {"type": "json_schema", "json_schema": {"schema": ...}}`, so llama-server can only produce output that matches the schema. Structure errors become impossible, and only the *content* (the quotes) still needs verifying.
4. The bridge checks each claim. It passes only if `quote`, after whitespace is normalized, appears within lines `[line-2, line+2]` of `file`.
5. Claims that fail are **removed**, and `stripped` counts them. If `answer` relies on a removed claim, the report says `"answer_confidence": "degraded"`.
6. Output that isn't valid JSON is retried once. If it fails again, the tool returns `{"error": "unverifiable_output"}` with no answer.
7. The tool description tells Claude: *before acting on a claim, read the cited lines yourself.*

### Write pipeline (mandatory)

1. Flint gets the current contents of `files` with line numbers, plus the task and constraints, and must return full replacement contents for each file it changes. It never returns diff hunks, because small models get hunks wrong.
   - **Region edits:** a file over `[budget].region_tokens` (default 2,000) isn't sent whole. The bridge splits it into top-level blocks, ranks them against the task and constraints the same way, and picks the file's header (its first block plus any import-only blocks after it) and at most 4 blocks scoring at least half the best, up to `region_tokens`. Neighbouring blocks are merged into one region, and each region ends at its last non-blank line so its text round-trips exactly. Flint sees an outline (the first line of every other block, numbered) and each `REGION` labelled with its id and line range, and returns the full new text of each region it changes. The bridge splices those in and builds the patch from the whole new file as usual, so the check and Claude's review of the patch don't change. An unknown region id gives `unexpected_region`, and whole-file content for a region file gives `unexpected_path`. If no block matches the task, the result is `over_budget` with a hint to name the function.
2. The bridge builds the patch in a temporary `git worktree` at `HEAD`:
   - It recreates the user's current state there: it applies `git diff HEAD`, copies untracked files, and symlinks `.venv` and `node_modules`.
   - It runs `git add -A` so that state becomes the baseline.
   - It writes Flint's files.
   - It takes `git diff --binary` as the patch.

   The patch is saved as `.puppet-master/staging/<patch_id>.patch` along with its sha256. The real working tree is **not** touched.
3. In the same worktree, the bridge runs the default allow-listed check (`[check].default` in `puppet-master.toml`) and records whether it passed or failed against the `patch_id`.
4. The report gives the diffstat, the check result, and `patch_path`. The description tells Claude: *read the patch at `patch_path` before calling `puppet_apply`.*
5. `puppet_apply` runs `git apply --check` and then `git apply` in the real working tree, but only if the check passed and the hash still matches.
6. A retry is `puppet_write` called again with `constraints` that include Claude's feedback. After 2 failed attempts on the same task (the bridge tracks a `task_key`), the tool returns `{"give_up": true}` and logs a **miss**.

### Fallbacks

- llama-server isn't reachable, or a request takes more than **90s**: the call is aborted and returns `{"error": "flint_offline"}`. Claude does the work itself and tells the user.
- Input bigger than the chunk budget across more than 8 chunks: the tool returns `{"error": "too_large"}` and suggests narrower paths.

### Server lifecycle (the bridge runs Flint)

The settings live in `~/.puppet-master/server.toml`, a machine-wide file that the user writes once. Claude can never pass flags to llama-server. It can only call `start`, `stop` or `restart`, and the bridge builds the command line from this file:

```toml
binary = "~/Projects/llama.cpp/build/bin/llama-server"
model  = "~/models/flint/flint-UD-IQ2_XXS.gguf"
host = "127.0.0.1"          # enforced: only 127.0.0.1 is accepted
port = 8000
ctx = 16384
parallel = 1                # -np: one slot gets the whole context
gpu_layers = 99
flash_attn = "on"
cache_type_k = "q8_0"       # KV cache quantization (about half the memory of f16)
cache_type_v = "q8_0"
cache_reuse = 256           # --cache-reuse: reuse KV cache across calls that share a prefix
reasoning_budget = 0        # --reasoning-budget: 0 = suppress thinking, -1 = unlimited
slot_save_path = "~/.puppet-master/slots"
autostart = true            # start on the first tool call if it's down
idle_stop_min = 15          # free ~12GB of RAM after 15 idle minutes
min_free_gb = 1.5           # memory headroom required beyond the model file
start_timeout_s = 180
```

- **Start:**
  - **Memory preflight.** Free memory comes from `memory_pressure -Q` × `hw.memsize`. If it's below `model file size + KV estimate + min_free_gb`, the bridge returns `{"error": "insufficient_memory", "need_gb", "free_gb", "hint": "close apps"}` and does **not** launch.
  - **Already running.** If something already answers `/health` on the port, the bridge reuses it with `managed: false` and never kills it.
  - **Launch.** Otherwise the bridge runs llama-server detached, writes `~/.puppet-master/server.pid` and logs to `~/.puppet-master/server.log`, then polls `/health` until it's ready or `start_timeout_s` runs out.
- **Autostart:** when another tool finds Flint down and `autostart = true`, the bridge starts it first. Startup time doesn't count toward the 90s request timeout.
- **Idle stop:** a background task stops a *managed* server after `idle_stop_min` with no calls, giving the memory back to macOS. The next call autostarts it again.
- **Stop:** the bridge sends SIGTERM to the pid in the pidfile, then SIGKILL after 10s. It only ever stops servers it started (identified by pid plus a command-line match).
- **Multiple sessions:** several Code-tab sessions each launch their own bridge, but they share one server through the pidfile and port. A lock file (`~/.puppet-master/server.lock`) prevents two starts at once.

### KV cache use (prompt-prefix reuse)

llama-server keeps the KV cache of the last prompt in the slot and reuses the longest matching prefix (`cache_prompt: true`, plus `--cache-reuse`). The bridge orders every prompt so that the large, stable parts come first:

1. The system prompt, which is fixed for each tool.
2. File contents, in sorted path order.
3. The question or task, **last**.

A second question about the same files then re-processes only the question, so a 10k-token prefix doesn't have to be reprocessed. With `-np 1` this works for consecutive calls. Every request sends `"cache_prompt": true`.

### Context budget (Claude sizes every delegation)

Flint has a 16,384-token window, and each call needs room for the prompt, the file content and the answer. Claude has to size each request to fit before sending it. Retrying calls that don't fit is not the plan.

| Budget | Value | Where it comes from |
|---|---|---|
| Total window | 16,384 tokens | `puppet_status().ctx` |
| File content per chunk | ~10,000 tokens (~40 KB, roughly 800–1,000 lines of code) | `[budget].chunk_tokens` |
| Chunks per `puppet_read` | ≤ 8 (~80k tokens of source in total) | hard limit |
| Focused read shown to Flint | ~3,000 tokens of the best-matching blocks | `[budget].focus_tokens` |
| Largest file text for focused reads | ~500,000 tokens (~2 MB) loaded and ranked | hard limit |
| Region edits | files over ~2,000 tokens, ≤ 2,000 tokens of regions per file | `[budget].region_tokens` |
| Editable text per `puppet_write` | ≤ 6,000 tokens: whole small files plus the regions of big ones | hard limit |
| Files per `puppet_write` | ≤ 3 | hard limit |

- **Exposed to Claude:** `puppet_status()` returns these budgets. Every tool description states its limit in one line, e.g. *"Max ~10k tokens of files per chunk, 8 chunks per call."*
- **Preflight:** before calling Flint, the bridge estimates the input size (`ceil(bytes/4)`).
  - Over the limit, it returns `{"error": "over_budget", "est_tokens", "limit", "hint"}` without calling the model. The hint suggests narrower paths, a line range, or splitting the task.
- **Claude's duties** (stated in the server instructions and `CLAUDE.md`):
  - Pass files, line ranges or globs, never whole directories. A big glob is fine: the read is searched.
  - Split big reads by question or by module.
  - Split a big write into one task per file, and name the function or class in the task so region edits pick the right blocks.
- **Line ranges:** `puppet_read` paths accept `path:start-end`, so Claude can send only the part that matters.

### Policy layers (who does the job)

1. The `FastMCP(name, instructions=...)` server instructions carry the delegate-first policy, in about 150 words.
2. Tool descriptions are written as directives, e.g. *"USE THIS FIRST to read or summarize files >200 lines or logs."*
3. **Per-project `CLAUDE.md`**: each project that uses Flint gets a `## Flint delegation` section in its own `CLAUDE.md`, copied from the template in `docs/spec/claude-md-snippet.md`. Nothing goes in the global `~/.claude/CLAUDE.md`, so delegation happens only in projects that opt in, and each project can list its own check commands. The section restates the policy:
   - **Delegate:** bulk reads, summaries, boilerplate, tests from a spec, docstrings, mechanical refactors, drafts.
   - **Keep:** planning, architecture, subtle bugs, security-sensitive code, and anything under about 200 output tokens.
   - **Skipping Flint:** when Claude doesn't delegate, it says why in one line.

### Call log

Each call appends one line to `.puppet-master/log.jsonl` with `{ts, tool, task_key, duration_ms, report_tokens, flint_tokens_in, flint_tokens_out, outcome, stripped, retries}`. The `outcome` field is one of `ok`, `check_failed`, `applied`, `rejected`, `miss`, or `offline`.

`uv run puppet-stats` summarizes the log: calls per tool, accept rate, average report tokens, misses.

## Commands

```
Venv:       uv venv --python 3.11            # .venv/ at project root (created)
Install:    uv sync
Run (MCP):  uv run puppet-master                 # stdio; registered in Claude Desktop MCP config
Test:       uv run pytest -q
Coverage:   uv run pytest --cov=puppet_master --cov-report=term-missing
Lint:       uv run ruff check . && uv run ruff format --check .
Stats:      uv run puppet-stats [--since 7d]
Smoke:      uv run pytest -m live               # needs llama-server on :8000
```

Example Claude Desktop MCP config entry:

```json
"puppet-master": {
  "command": "uv",
  "args": ["--directory", "/path/to/puppet-master", "run", "puppet-master"]
}
```

## Project Structure

```
src/puppet_master/
  server.py        → FastMCP app, server instructions, tool registration
  llm.py           → llama-server client (timeout, JSON-schema grammar, cache_prompt)
  lifecycle.py     → server.toml, memory preflight, start/stop, pidfile/lock, idle stop
  read.py          → line numbering, chunking, map-reduce, focused reads, claim verification
  retrieve.py      → block splitting, BM25 ranking, definition expansion
  write.py         → full-file and region generation, splicing, staging, apply
  check.py         → allow-list loading, worktree runner, failure extraction
  report.py        → 500-token cap, truncation
  log.py, stats.py → JSONL call log + summary CLI
tests/             → unit tests (mocked LLM) + tests/live/ (marker: live)
docs/intent/       → confirmed intent
docs/spec/         → this spec + CLAUDE.md snippet
puppet-master.toml         → per-project config (allow-list, budgets) — lives in target project
.puppet-master/            → staging/ + log.jsonl (gitignored, in target project)
```

`puppet-master.toml` example:

```toml
[check]
default = "test"
[check.commands]
test = ["uv", "run", "pytest", "-q", "-x"]
lint = ["uv", "run", "ruff", "check", "."]
[budget]
chunk_tokens = 10000
timeout_s = 90
focus_tokens = 3000   # focused reads
region_tokens = 2000  # region edits
```

## Code Style

The code is typed, uses small pure functions, and keeps I/O at the edges. Every tool returns its result through `report()`. Formatting and linting come from ruff defaults with a line length of 100.

```python
def verify_claim(claim: Claim, lines: list[str], window: int = 2) -> bool:
    """True if claim.quote appears within ±window lines of claim.line."""
    lo = max(0, claim.line - 1 - window)
    hi = min(len(lines), claim.line + window)
    haystack = _norm(" ".join(lines[lo:hi]))
    return bool(claim.quote.strip()) and _norm(claim.quote) in haystack
```

## Testing Strategy

- **Unit tests (most of the suite):** run with `respx` mocking llama-server.
  - Claim verification covers a quote that's off by one line, an invented file, an empty quote, and whitespace variations.
  - Other units: chunk boundaries, report truncation, allow-list rejection, patch hashing, apply refusal.
- **Integration tests:** run against a throwaway git repo in `tmp_path`.
  - A `puppet_write` produces a patch and leaves the working tree unchanged.
  - A failing check blocks `puppet_apply`.
  - A patch edited after its check blocks `puppet_apply`.
  - Two failed attempts are logged as a miss.
- **Adversarial fixtures:** canned "hallucinating" Flint responses (made-up functions, wrong line numbers, quotes that don't match) must all be stripped.
- **Live smoke tests (`-m live`):** one round trip per tool against the real server. These aren't run in the default suite.
- **Coverage:** ≥ 90% on `read.py`, `write.py`, `check.py`, and `report.py`. These modules are the guardrails.

## Boundaries

- **Always:**
  - Verify every read claim mechanically.
  - Stage writes and never touch the working tree outside `puppet_apply`.
  - Run `subprocess` with argument lists only (no shell).
  - Cap every report at 500 tokens.
  - Log every call.
- **Ask first:**
  - Adding dependencies.
  - Changing the report schema or tool signatures.
  - Relaxing any verification rule (the window size, normalization).
  - Adding a write path that skips checks.
- **Never:**
  - Return raw file contents or full Flint output to Claude.
  - Let Flint run arbitrary commands.
  - Apply a patch whose check failed or whose hash changed.
  - Make checks optional or configurable off.
  - Send code or data anywhere except `127.0.0.1`.
  - Pass Claude-supplied flags or paths to llama-server, or bind it to anything other than `127.0.0.1`.
  - Stop a llama-server the bridge didn't start.

## Success Criteria

1. Claude Desktop's Code tab lists the `puppet-master` tools, and `puppet_status` returns `up: true`.
2. On the adversarial fixtures, **100%** of fabricated claims are removed, and no unverified claim reaches a `puppet_read` report.
3. `puppet_apply` refuses in every test case where the check failed, the hash doesn't match, or the patch is missing.
4. Every report is ≤ 500 tokens (asserted in tests, measured in the log).
5. `uv run pytest -q` and `ruff check` pass, and the guardrail modules have ≥ 90% coverage.
6. One-week trial (measured, not gated):
   - delegation rate ≥ 60% of eligible tasks
   - accept rate ≥ 70% within 2 attempts
   - zero Flint-originated defects found in the repo
   - fewer limit hits than the week before

## Model choice and fine-tuning

- **Model:** Flint UD-IQ2_XXS is the default, provisional until the T13 bake-off against Gemma 4 12B Q4. See [ADR 0001](../decisions/0001-local-model-choice.md).
- **Fine-tuning:** not in v1. It's the last resort, with logged triggers to reopen it. See [ADR 0002](../decisions/0002-fine-tuning-last-resort.md).

### Reasoning control

Flint is a reasoning model. Its chat template has no `enable_thinking` switch, and the model decides on its own whether to emit `<think>`. Thinking tokens use up the same 16K window and `max_tokens` budget as the files and the answer.

- **Default:** `server.toml` sets `reasoning_budget = 0`, which is passed as `--reasoning-budget 0`. The bridge also strips any `reasoning_content` or `<think>…</think>` from replies before parsing.
- **Measured:** the bridge logs `reasoning_tokens` for every call, using `usage.completion_tokens_details.reasoning_tokens` when present, or an estimate from the stripped text.
- **Fallback:** if Checkpoint A shows thinking can't be suppressed, or that suppressing it makes the answers worse, set `reasoning_budget = -1` (unlimited) plus `reasoning_reserve = 3000` in `puppet-master.toml`. The bridge subtracts the reserve from `chunk_tokens` and adds it to `max_tokens`.
- **Grammar:** the JSON schema has to apply to the answer, not to the thinking. Checkpoint A verifies that `json_schema` and the reasoning setting work together.

### Sample capture (keeps fine-tuning possible)

When `samples = true` (the default) in `puppet-master.toml`, every Flint call writes `{tool, system, user, raw_output, verdict}` to `.puppet-master/samples/YYYY-MM-DD.jsonl` in the target project. The `verdict` is one of `verified`, `stripped:N`, `check_failed`, `applied`, `rejected` or `unverifiable`.

The samples stay local, `.puppet-master/` is gitignored, and the files are capped at 200MB in total (oldest deleted first). These samples are the only training data ADR 0002 would use.

## Open Questions

1. ~~Dependencies in the check worktree~~ **Resolved:** projects keep a uv `.venv` at the project root, and the check worktree symlinks it (and `node_modules` if present) from the main tree.
2. ~~Large files in `puppet_write`~~ **Resolved (revised 2026-10-25):** files over ~2k tokens are edited by region (see Write pipeline). See [ADR 0003](../decisions/0003-bm25-focus-and-region-edits.md).
3. ~~`puppet_ask` scope~~ **Resolved:** `puppet_ask` is for drafts and prose only. Its description sends questions about code to `puppet_read`, and its output is always `verified: false`.
4. ~~The Flint model and llama.cpp PR~~ **Resolved:** PRs #30928 and #30929 were merged 2026-10-21, so llama.cpp is built from master. The model is `unsloth/flint-GGUF` `UD-IQ2_XXS` (11.86 GB), because Q2_K_XL and IQ2_M don't fit in 18GB. See [docs/setup/flint-server.md](../setup/flint-server.md).
5. ~~Where `CLAUDE.md` goes~~ **Resolved:** per project. See Policy layers.
