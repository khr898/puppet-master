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
