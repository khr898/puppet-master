# flint-bridge

A stdio MCP server (`flint-engine`) that lets Claude hand bulk reads, writes, checks and drafts to a
local llama-server ("Flint"), with every guardrail enforced in code. See
[docs/spec/SPEC.md](docs/spec/SPEC.md) and [tasks/plan.md](tasks/plan.md).

## Develop

```bash
UV_CACHE_DIR=.uv-cache uv sync
UV_CACHE_DIR=.uv-cache uv run pytest -q
UV_CACHE_DIR=.uv-cache uv run ruff check . && UV_CACHE_DIR=.uv-cache uv run ruff format --check .
```

## Register the server

Flint itself is set up once by following [docs/setup/flint-server.md](docs/setup/flint-server.md).

**Claude Desktop** — add to the MCP config JSON, then open a new Code-tab session:

```json
"flint-engine": {
  "command": "uv",
  "args": ["--directory", "/path/to/puppet-master", "run", "flint-bridge"]
}
```

**Claude Code CLI:**

```bash
claude mcp add flint-engine -- uv --directory /path/to/puppet-master run flint-bridge
```

## Using it and running the trial

Once registered, Claude sees the Flint tools in every session. Copy the `## Flint delegation`
section of [CLAUDE.md](CLAUDE.md) (template: [docs/spec/claude-md-snippet.md](docs/spec/claude-md-snippet.md))
into the `CLAUDE.md` of each project that should use Flint, and add a `flint.toml` with that
project's check commands (this repo's own is an example).

Every tool call is logged to `.flint/log.jsonl` in the project. Read the log with:

```bash
UV_CACHE_DIR=.uv-cache uv run flint-stats --since 7d
```

It prints calls per tool, outcome counts, the accept rate (`applied` over `applied`, `rejected`,
`miss` and `check_failed`), average report size, Flint tokens in and out, average duration and
average reasoning tokens.

It also prints the **measured savings** from real calls. Each row logs what the call cost Claude
(`cost_in`: the report plus the cited lines or patch Claude reads to check it; `cost_out`: the
arguments Claude wrote) and, for delivered work, what Claude would have spent doing it itself
(`avoided_in`: the file text Flint actually saw, so a focused read of a broad glob counts only the
matching blocks; `avoided_out`: the added lines of an applied patch, or an `flint_ask` draft). A
write is credited only when applied, so rejected and failed delegations count against Flint. The
net is shown with output tokens weighted 5x (their price ratio). All figures are `chars/4`
estimates, and rows from before this was measured are listed as unmeasured. Accuracy is the
share of claims stripped by verification and the number of unverifiable outputs.

Reports reach Claude as compact JSON, and a read's claims give their shared folder once as
`base` with each `file` relative to it.

For the one-week trial, use Claude as usual for a week and compare how
often you hit your usage limit with a normal week, alongside the accept rate and the misses.

Smoke-test the real model with `uv run pytest -m live`; it skips itself when llama-server is down.
Local training samples (`.flint/samples/`) can be switched off with `[log] samples = false`.

## Choosing the model (the bake-off)

`flint-eval` scores whichever model llama-server is running on the bridge's own tasks (10 read
questions about this repo, 5 small fix-the-failing-test write tasks in `evals/`), using the real
read and write code, so verified claims and the staged-patch checks are part of the score.

```bash
UV_CACHE_DIR=.uv-cache uv run flint-eval --label flint-iq2xxs     # model set in ~/.flint/server.toml
# change `model` in ~/.flint/server.toml, run flint_server("restart") from Claude, then:
UV_CACHE_DIR=.uv-cache uv run flint-eval --label gemma4-12b-q4
UV_CACHE_DIR=.uv-cache uv run flint-eval --compare flint-iq2xxs gemma4-12b-q4
```

Each run is saved to `evals/results/<label>.json`. Record the winner in
[ADR 0001](docs/decisions/0001-local-model-choice.md).
