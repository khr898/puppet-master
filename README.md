# Puppet Master

**A research concept: cut the tokens, and the cost, of agentic development by letting a cheap local
model do Claude's bulk work.**

Puppet Master is a stdio [MCP](https://modelcontextprotocol.io) server. Claude stays the planner and
reviewer; the bulk reading, first-draft writing and mechanical edits go to a small model that runs
on your own machine (or any OpenAI-compatible endpoint). Every guardrail is enforced in code, not in
a prompt:

- **Reads** return only claims whose quoted lines were found in the file. Unverified claims are
  stripped before Claude sees them.
- **Writes** are staged as a patch, run through your project's test/lint checks in a throwaway
  worktree, and reach the repo only through `puppet_apply`, which refuses a patch whose check failed.
- **Budgets** keep every request inside the worker's context window, with BM25 retrieval to hand it
  only the blocks of a large file that match the question.

This is a research prototype, not a finished product. The measured results below come from one
small machine, one default model and a 27-task benchmark of this repo. The design is in
[docs/spec/SPEC.md](docs/spec/SPEC.md) and the decision records are in [docs/decisions](docs/decisions).

> **Accuracy depends on the worker model.** Reading and writing accuracy rises with a better local
> model: a larger model, a higher-fidelity quantization, or a reasoning model with thinking on.
> The numbers below are for a heavily quantized model that fits an 18GB laptop, which is close to
> the floor. Better models are expected to do better, and the bake-off tool (`puppet-eval`) lets you
> measure that on your own hardware. See [Using other models](#using-other-models-and-servers).

## Results

All figures come from the saved runs in [evals/results](evals/results) and from the test suite.

### Test suite

| | |
|---|---|
| Tests | **357 passed**, 22 skipped, 4 live tests deselected (they need a running model server) |
| Skipped | Port-binding tests, which skip themselves inside a sandbox that forbids binding a port |
| Line coverage | **89%** of 1,847 statements (`pytest --cov`) |
| Lint | `ruff check` and `ruff format --check` clean |

### Benchmark: the worker model on this repo's own tasks

`puppet-eval` runs 20 read questions and 7 "fix the failing test" write tasks through the real read
and write code, so claim verification and the staged-patch checks count toward the score.
`puppet-savings` estimates what Claude would have spent doing the same work itself
(`chars / 4` tokens, output tokens weighted 5x).

Worker model: **Flint** `UD-IQ2_XXS` (a 40B-A2.5B mixture-of-experts model, 2-bit quantization,
11.86 GB), run by llama.cpp at temperature 0, 16K context, thinking off.

| Run | Read answers correct | Write checks pass | Claude tokens saved | Speed |
|---|---|---|---|---|
| `flint-iq2xxs-p6` | 90% (18/20) | 86% (6/7) | **74.1%** (reads 58.5%, writes 88.2%) | 26.5 tok/s |
| `phase6-concrete` (re-run) | 85% (17/20) | 86% (6/7) | **74.2%** (reads 57.8%, writes 89.0%) | 26.4 tok/s |

Across both runs there were 0 errors, 0 unverifiable outputs and 0 reasoning tokens. The 5-point
difference between the runs is run-to-run noise on 20 questions, so read it as roughly 85-90%.
Savings are **estimates** from character counts, not billed-token measurements, and they cover
delegable work only; planning and review stay with Claude.

A second model, for contrast (JetBrains Mellum2.1 12B-A2.5B, Q4_K_M, 8.1 GB), on the same tasks:

| | Flint IQ2_XXS | Mellum2.1, thinking off | Mellum2.1, thinking on |
|---|---|---|---|
| Read answers correct | **90%** | 60% | 85% |
| Write checks pass | **86%** | 71% | 71% |
| Claude tokens saved | **74%** | 58% | 57% |
| Avg model time per read | 7.8 s | 7.0 s | 89 s |

The same tool gave very different accuracy on different models and settings, which is the point:
the guardrails keep a weak worker safe, but a stronger worker means fewer stripped claims and more
patches that pass. No larger or higher-precision model has been benchmarked yet.

### Test machine

| | |
|---|---|
| Machine | Apple Silicon Mac with an **M3 Pro** chip |
| Memory | **18 GB** unified (about 12 GB usable by the GPU by default) |
| OS | macOS (Darwin 27) |
| Inference | llama.cpp `llama-server`, Metal, flash attention, 8-bit KV cache, 16,384-token context |
| Model | `flint-UD-IQ2_XXS.gguf` from `unsloth/flint-GGUF`, 11.86 GB |

## Installation

An AI agent doing this setup should follow [INSTALL_FOR_AGENTS.md](INSTALL_FOR_AGENTS.md).

You need Python 3.11+ (developed and tested on macOS; Linux is untested), [`uv`](https://docs.astral.sh/uv/), `git`, and a model server
(step 2).

### 1. Get the code

```bash
git clone https://github.com/khr898/puppet-master.git
cd puppet-master
uv sync
```

### 2. Run a worker model

Pick one. All of them speak the OpenAI-style `/v1/chat/completions` API.

- **Local, managed by Puppet Master (llama.cpp):** follow
  [docs/setup/flint-server.md](docs/setup/flint-server.md). Puppet Master then starts and stops
  `llama-server` on demand and frees the memory when idle.
- **Local, you run the server:** Ollama, LM Studio, vLLM, mlx_lm or `llama-server` yourself.
- **Hosted:** OpenRouter or any OpenAI-compatible API.

The last two are covered in [Using other models](#using-other-models-and-servers).

### 3. Register the MCP server

**Claude Code CLI:**

```bash
claude mcp add puppet-master -- uv --directory "$(pwd)" run puppet-master
```

**Claude Desktop**: add this to the MCP config JSON (use the absolute path to your clone), then open
a new Code-tab session:

```json
"puppet-master": {
  "command": "uv",
  "args": ["--directory", "/path/to/puppet-master", "run", "puppet-master"]
}
```

### 4. Turn it on in a project

1. Copy the `## Flint delegation` section of
   [docs/spec/claude-md-snippet.md](docs/spec/claude-md-snippet.md) into that project's `CLAUDE.md`
   (or `AGENTS.md`). It tells the agent what to delegate and what to keep.
2. Add a `puppet-master.toml` with the project's check commands. This repo's own file is an example:

   ```toml
   [check]
   default = "test"

   [check.commands]
   test = ["uv", "run", "pytest", "-q", "-x"]
   lint = ["uv", "run", "ruff", "check", "."]
   ```

   Only commands listed here can run, as argument lists with no shell.
3. Add `.puppet-master/` to the project's `.gitignore`. Logs, staged patches and attempt counts live
   there.

### 5. Verify

In a new Claude session, ask it to call `puppet_status`. It should report the worker as up (or
startable) with its token budgets. Then check the numbers after some real use:

```bash
uv run puppet-stats --since 7d
```

## Tools

| Tool | What it does |
|---|---|
| `puppet_status` | Worker health and the current token budgets |
| `puppet_read` | Summarize or search files, line ranges or globs; returns only verified claims |
| `puppet_write` | Draft edits to up to 3 files; stages a patch and runs the check |
| `puppet_check` | Re-run a project check on a staged patch |
| `puppet_apply` | Apply a staged patch, only if its check passed |
| `puppet_reject` | Discard a staged patch and record the miss |
| `puppet_ask` | Free-form draft (commit message, docstring); treat as unverified |
| `puppet_server` | Start, stop or restart the managed llama-server (not for hosted APIs) |

## Using other models and servers

Puppet Master talks to **any server that exposes `POST /v1/chat/completions`**. Three settings
select it, and each can be set by environment variable or in `puppet-master.toml`:

| Setting | Environment variable | `puppet-master.toml` | Default |
|---|---|---|---|
| Server URL, without `/v1` | `PUPPET_BASE_URL` | `[server] base_url` | `http://127.0.0.1:8000` |
| Model id sent with each request | `PUPPET_MODEL` | `[server] model` | `flint` |
| API key (hosted APIs) | `PUPPET_API_KEY` | not supported, on purpose | none |

Environment variables win over the file. The API key is read **only** from the environment and is
sent as `Authorization: Bearer <key>`, so it can never be committed with a project config. Put it in
the MCP registration (below) or your shell profile, never in a file inside a repo.

Set the model's real context window and budgets in `puppet-master.toml` so requests are sized to
fit. If these are too high for the model, requests fail or get truncated:

```toml
[server]
ctx = 32768            # the context the server actually gives the model
temperature = 0        # keeps quotes and edits reproducible

[budget]
chunk_tokens = 20000   # file tokens per read chunk; keep well under ctx
focus_tokens = 6000    # tokens a focused (BM25) read shows the model
```

Pass the environment variables when registering the server. With the CLI:

```bash
claude mcp add puppet-master \
  -e PUPPET_BASE_URL=http://127.0.0.1:11434 \
  -e PUPPET_MODEL=qwen2.5-coder:14b \
  -- uv --directory "$(pwd)" run puppet-master
```

In Claude Desktop's JSON, add `"env": { "PUPPET_BASE_URL": "...", "PUPPET_MODEL": "..." }` beside
`command` and `args`.

### Recipes

| Server | Start it | `PUPPET_BASE_URL` | `PUPPET_MODEL` |
|---|---|---|---|
| **llama.cpp** (managed) | Puppet Master starts it from `~/.puppet-master/server.toml` | `http://127.0.0.1:8000` | the `--alias` you set |
| **Ollama** | `ollama serve`, then `ollama pull <model>` | `http://127.0.0.1:11434` | the tag, e.g. `qwen2.5-coder:14b` |
| **LM Studio** | Start the local server in the app | `http://127.0.0.1:1234` | the id shown in the app |
| **vLLM** | `vllm serve <model>` | `http://127.0.0.1:8000` | the `<model>` you served |
| **mlx_lm** | `mlx_lm.server --model <model>` | `http://127.0.0.1:8080` | the `<model>` you served |
| **OpenRouter** | Create a key at openrouter.ai | `https://openrouter.ai/api` | any model id from their catalog |
| **Other hosted API** (OpenAI-compatible) | Get a key from the provider | the provider's base URL, minus `/v1` | the provider's model id |

For a hosted API, add `-e PUPPET_API_KEY=...` to the registration command (it is stored in your
local Claude config, not in this repo).

Notes:

- **Ollama's default context is small.** Set a larger one (for example `OLLAMA_CONTEXT_LENGTH=16384`
  in the server's environment, or `num_ctx` in a Modelfile) and match `ctx` in `puppet-master.toml`.
- **Unmanaged servers:** `~/.puppet-master/server.toml` is only for the managed llama.cpp server.
  With any other server, skip it. `puppet_server` then reports `no_server_config`, which is expected,
  and health is checked through `/health` or `/v1/models`.
- **Structured output:** reads and writes ask for JSON-schema output (`response_format`). A server
  that ignores it still works, because replies are validated in code and a bad one is retried once,
  then reported, but a server with grammar-constrained decoding (llama.cpp, vLLM) gives the cleanest
  results.
- **Reasoning models:** thinking tokens share the context window. Disable thinking on the server
  where you can, or set `[budget] reasoning_reserve` so chunks leave room for it.
- **Privacy and cost of hosted APIs.** With a hosted endpoint, every file the worker reads leaves
  your machine for a third party, and each call costs money. That can cancel the savings and may
  break your code-sharing rules. Use a local server for private code.

### Measure a model before trusting it

`puppet-eval` scores whichever model the configured server is running, on this repo's tasks (20 read
questions about this repo and 7 small fix-the-failing-test write tasks in [evals](evals)):

```bash
uv run puppet-eval --label my-model
uv run puppet-eval --compare flint-iq2xxs my-model
uv run puppet-savings --label my-model-savings     # runs the model: estimated Claude tokens saved
uv run puppet-savings --show flint-iq2xxs-savings   # print a saved run without running a model
```

Results are saved to `evals/results/<label>.json`. Record the winner in
[ADR 0001](docs/decisions/0001-local-model-choice.md).

## Reading the log

Every tool call is appended to `.puppet-master/log.jsonl` in the project. `puppet-stats` prints calls
per tool, the accept rate, report sizes, worker tokens, durations, and the measured Claude-token
savings: each call logs what it cost Claude (the report plus the cited lines or patch Claude reads
to check it) and, for delivered work, what Claude would have spent itself. A write counts as saved
only when applied, so rejected and failed delegations count against the worker. All figures are
`chars / 4` estimates. Local training samples (`.puppet-master/samples/`) can be switched off with
`[log] samples = false`.

## Develop

```bash
uv sync
uv run pytest -q                      # 357 tests; live tests are deselected
uv run pytest -m live                 # needs a model server on :8000
uv run ruff check . && uv run ruff format --check .
```

In a sandbox that blocks uv's default cache, prefix each command with `UV_CACHE_DIR=.uv-cache`.

## Limits

- One benchmark, one machine, one default model. The savings are estimates, not billed tokens.
- Not yet measured: models larger or less quantized than the default, and a multi-week trial of real
  usage against a Claude usage limit.
- The checks run your project's own test and lint commands, so they are only as good as your tests.

## License

[MIT](LICENSE)
