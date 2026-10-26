# Installing Puppet Master: instructions for AI agents

You are an AI agent asked to set up Puppet Master, an MCP server that lets a planner model (Claude)
hand bulk reads, writes and checks to a cheaper worker model. Follow these steps in order. After each
step, run its **Verify** command and continue only if it passes. If a step fails, read
[Troubleshooting](#troubleshooting) before retrying, and stop and tell the user if you are still
stuck after two attempts.

## Rules

1. **Never write an API key, token or password into any file.** Keys go only in environment
   variables or in the MCP client's own registration (`-e PUPPET_API_KEY=...`). If the user has not
   given you a key, ask them to supply it; do not look for one on the machine.
2. **Do not use `sudo`** or change system settings (for example the GPU memory limit). If a step
   seems to need it, tell the user and let them decide.
3. **Ask before choosing the worker model.** Local (private, free) or hosted (sends the user's code
   to a third party, costs money) is the user's decision (step 2).
4. **Use absolute paths** when registering the server.
5. **Do not commit** anything the setup creates (`.puppet-master/`, logs, patches).
6. Report what you did and what you verified. Do not claim a step passed without running its Verify.

## Step 0: Prerequisites

Check, and install only what is missing (ask first if it needs a package manager the user hasn't
used):

```bash
python3 --version     # 3.11 or newer
uv --version          # https://docs.astral.sh/uv/
git --version
```

Developed and tested on macOS. Linux is untested; the memory preflight for the managed llama.cpp
server reads macOS tools, so on Linux use an unmanaged server (step 2, options B to D).

## Step 1: Install

```bash
git clone https://github.com/khr898/puppet-master.git
cd puppet-master
uv sync
uv run pytest -q
```

**Verify:** `uv sync` ends without error and pytest reports `passed` (about 355 passed, 22 skipped
is normal; the skips are tests that need a free network port). If the user's sandbox blocks uv's
cache, prefix commands with `UV_CACHE_DIR=.uv-cache`.

Note the absolute path of the clone (`pwd`). Call it `$PM_DIR`.

## Step 2: Choose and start a worker model

Ask the user which option they want. Option A is the default tested setup.

### A. Local llama.cpp, managed by Puppet Master (tested)

Follow [docs/setup.md](docs/setup.md) (the worked example and the optional managed-server section): build llama.cpp, download the model
(about 12 GB; confirm disk space and get the user's OK before downloading), and write
`~/.puppet-master/server.toml`. Puppet Master then starts and stops the server itself.

**Verify:** after the manual test run in that document, `curl -s http://127.0.0.1:8000/health`
returns `{"status":"ok"}`. Stop the test server afterwards (Ctrl-C).

### B. Ollama, LM Studio, vLLM or mlx_lm (you or the user run the server)

Start the server and load a model, then record these two values:

| Server | `PUPPET_BASE_URL` |
|---|---|
| Ollama | `http://127.0.0.1:11434` |
| LM Studio | `http://127.0.0.1:1234` |
| vLLM | `http://127.0.0.1:8000` |
| mlx_lm | `http://127.0.0.1:8080` |

`PUPPET_MODEL` is the model name the server uses (for Ollama the tag, for example one shown by
`ollama list`).

**Verify:**

```bash
curl -s "$PUPPET_BASE_URL/v1/models"
```

lists the model. Do not create `~/.puppet-master/server.toml` for these servers.

For Ollama, the default context window is small. Ask the user to set a larger one (for example
`OLLAMA_CONTEXT_LENGTH=16384` for the server process) and use that same number as `ctx` in step 4.

### C. OpenRouter

Only after the user confirms they accept that their code will be sent to OpenRouter and billed:

- `PUPPET_BASE_URL=https://openrouter.ai/api`
- `PUPPET_MODEL=<a model id from the OpenRouter catalog the user chose>`
- `PUPPET_API_KEY=<the user's key>` (supplied by the user, never written to a file)

### D. Any other OpenAI-compatible API

Use the provider's base URL without the trailing `/v1`, the provider's model id, and
`PUPPET_API_KEY` if it needs one. The same privacy rule as option C applies to hosted providers.

## Step 3: Register the MCP server

The server name is `puppet-master`. Pick the client the user uses.

### Claude Code CLI

Option A (managed llama.cpp, no env needed):

```bash
claude mcp add puppet-master -- uv --directory "$PM_DIR" run puppet-master
```

Options B to D (add one `-e` per setting; omit `PUPPET_API_KEY` unless the server needs it):

```bash
claude mcp add puppet-master \
  -e PUPPET_BASE_URL=<base url> \
  -e PUPPET_MODEL=<model id> \
  -e PUPPET_API_KEY=<key, only if needed> \
  -- uv --directory "$PM_DIR" run puppet-master
```

**Verify:** `claude mcp list` shows `puppet-master` as connected.

### Claude Desktop

Add this to the MCP config JSON (Claude Desktop, Settings, Developer, Edit Config), using the real
absolute path. Add an `"env"` object beside `"args"` for options B to D.

```json
"puppet-master": {
  "command": "uv",
  "args": ["--directory", "/absolute/path/to/puppet-master", "run", "puppet-master"],
  "env": {
    "PUPPET_BASE_URL": "http://127.0.0.1:11434",
    "PUPPET_MODEL": "your-model"
  }
}
```

Then restart the app and open a new session. Merge into the existing JSON; do not overwrite other
servers.

**Verify:** in a new session, the tools `puppet_status`, `puppet_read`, `puppet_write`,
`puppet_check`, `puppet_apply`, `puppet_reject`, `puppet_ask` and `puppet_server` are listed.

## Step 4: Enable it in the user's project

In the **target project** (not this repo):

1. Create `puppet-master.toml` in the project root:

   ```toml
   [check]
   default = "test"

   [check.commands]
   test = ["<the project's test command as an argument list>"]
   lint = ["<the project's lint command as an argument list>"]

   # Only for options B to D. Match what the server really gives the model.
   [server]
   ctx = 16384

   [budget]
   chunk_tokens = 10000
   ```

   Commands are argument lists with no shell, for example `["npm", "test"]`. Keep `chunk_tokens`
   well below `ctx`.
2. Add `.puppet-master/` to the project's `.gitignore`.

**Verify:** the project's own test command, run exactly as written in `puppet-master.toml`, passes
on a clean checkout. A failing baseline makes every patch check fail.

## Step 5: End-to-end check

In a new session in the target project, call `puppet_status`.

**Verify:** it returns without an `error` field and reports the worker as up (or startable for
option A) with token budgets. Then call `puppet_read` on one small file with a simple question; the
reply should contain claims with file, line and quote.

Finish with:

```bash
cd "$PM_DIR" && uv run puppet-stats --since 1d
```

which should list the calls you just made.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `claude mcp list` shows failed | Wrong path. Re-run with the absolute `$PM_DIR`; run `uv run puppet-master` there by hand (it should sit waiting on stdin; Ctrl-C to quit). |
| `puppet_status` says the worker is down | The server isn't running or `PUPPET_BASE_URL` is wrong. Option A: ask Claude to call `puppet_server("restart")`. Options B to D: `curl "$PUPPET_BASE_URL/v1/models"`. |
| `flint_offline` in a result | Timeout, connection refused, or a reply that couldn't be parsed. Check the server log; raise `[budget] timeout_s` for slow hosted models. |
| `401` or `403` from a hosted API | Missing or wrong `PUPPET_API_KEY`. Ask the user; never guess or search for one. |
| `over_budget` | The request exceeds the model's context. Lower `chunk_tokens` or `ctx` to match the server, or split the request. |
| `no_server_config` from `puppet_server` | Expected for options B to D, which are not managed. |
| Every patch fails its check | The project's baseline tests fail. Fix that first. |
| Memory preflight refuses to start (option A) | Not enough free RAM. Ask the user to close heavy apps or lower `ctx` in `~/.puppet-master/server.toml`. |

## Report back

Tell the user: which option you set up, the base URL and model id (never the key), the commands you
ran, which Verify steps passed, and anything you skipped or could not check.
