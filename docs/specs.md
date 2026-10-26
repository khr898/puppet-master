# Specs

How Puppet Master works. The goal: let a planner model (such as Claude) hand bulk reading and
writing to a cheaper worker model, **without any worker mistake reaching your project**. Every
guardrail below is enforced in code, not left to a prompt.

## Shape

- One Python MCP server over stdio (Python 3.11+, `uv`, the `mcp` SDK, `httpx`).
- The worker is any model behind an OpenAI-compatible `POST /v1/chat/completions`: llama.cpp,
  Ollama, LM Studio, vLLM, mlx_lm or a hosted API.
- Target projects are git repositories. Staging and checks use `git worktree` and `git apply`.
- Token counts are estimates: `ceil(chars / 4)`, not a tokenizer.

## Tools

Every tool returns a JSON report capped at **500 tokens**. Raw file contents and full worker output
never go back to the planner. All paths are absolute.

| Tool | Does |
|---|---|
| `puppet_status` | Worker health, context size and the current token budgets |
| `puppet_read(paths, question)` | Answers a question about files, line ranges (`path:start-end`) or globs |
| `puppet_write(task, files, constraints)` | Drafts edits and stages them as a patch; runs the project check |
| `puppet_check(cmd, root, patch_id?)` | Runs an allow-listed command; returns only the failures |
| `puppet_apply(root, patch_id)` | Applies a staged patch. Refuses unless its check passed and its hash is unchanged |
| `puppet_reject(root, patch_id, reason)` | Deletes a staged patch and counts a failed attempt |
| `puppet_ask(prompt, context)` | Free-form draft, always marked unverified |
| `puppet_server(action)` | `start`, `stop` or `restart` an optional managed llama.cpp server |

## Reads are verified

1. The server reads the files and prefixes each line with its number.
2. The worker must answer in JSON: `{answer, claims: [{text, file, line, quote}]}`. Where the server
   supports it the request uses grammar-constrained `json_schema` output, so only the quotes need
   checking.
3. A claim passes only if its `quote` (whitespace normalized) appears within two lines of the cited
   line. Failing claims are removed and counted in `stripped`; if the answer relied on one, the
   report says `answer_confidence: "degraded"`.
4. Invalid JSON is retried once, then the tool returns `unverifiable_output`.
5. Reads that need more than one chunk are searched, not streamed: BM25 picks the best-matching
   blocks (see [results.md](results.md)). `exhaustive=true` reads everything, up to 8 chunks.

The planner is told to read the cited lines before acting on a claim.

## Writes are staged and checked

1. The worker sees the files with line numbers and returns **full replacement contents** (never
   diff hunks, which small models get wrong). Files over `region_tokens` are edited by region: the
   worker sees an outline and only the blocks matching the task.
2. The server builds the patch in a temporary `git worktree` that recreates your current working
   state (uncommitted changes, untracked files, linked `.venv`/`node_modules`). Your real working
   tree is untouched.
3. In that worktree it runs the project's default check (`puppet-master.toml`, `[check]`) and
   records pass or fail against the patch id.
4. The patch is saved to `.puppet-master/staging/<id>.patch` with its sha256. The report carries the
   diffstat, the check result and the path; the planner reads the patch before applying.
5. `puppet_apply` runs `git apply --check` then `git apply`, only if the check passed and the hash
   matches. This is the only route into the repo.
6. After two failed attempts on one task the tool returns `give_up` and logs a miss.

Checks are an allow-list of argument lists (no shell) from `puppet-master.toml`.

## Budgets

The worker has a fixed context window, so every request is sized before it is sent. Over the limit
returns `over_budget` with a hint, without calling the model. Defaults (`puppet_status` reports the
live values):

| Budget | Default |
|---|---|
| Context window | `ctx`, 16,384 tokens |
| File text per read chunk | `chunk_tokens`, 10,000 |
| Chunks per read | up to 8 |
| Focused read shows the worker | `focus_tokens`, 3,000 |
| Region edit threshold | `region_tokens`, 2,000 |
| Editable text per write | 6,000 tokens, up to 3 files |
| Request timeout | `timeout_s`, 90 s |

Prompts put stable parts first (system prompt, then files in sorted order, then the question), so a
server with prompt caching only reprocesses the question on a follow-up.

## Failure behavior

- Worker unreachable, timed out or unparseable reply: `{"error": "flint_offline"}`. The planner does
  the work itself and tells the user.
- More input than 8 chunks: `too_large`.
- Not enough free memory for the managed server: `insufficient_memory`; nothing is launched.

## Optional managed llama.cpp server

If `~/.puppet-master/server.toml` exists, the server can launch `llama-server` itself (see
[setup.md](setup.md)). The planner can only call `start`, `stop` or `restart`; it can never pass
flags. Only `127.0.0.1` is accepted. Before starting it checks free memory against model size plus
KV cache plus `min_free_gb`; it reuses an already-running server without ever killing it; it stops a
server it started after `idle_stop_min` idle minutes (SIGTERM, then SIGKILL after 10 s); and a lock
file stops two sessions starting it at once.

## Call log

Every call is appended to `.puppet-master/log.jsonl`: tool, outcome, report size, worker tokens,
duration, and the planner-token cost and savings. `puppet-stats` summarizes it. Local training
samples (`.puppet-master/samples/`, capped at 200 MB) can be turned off with `[log] samples = false`.
