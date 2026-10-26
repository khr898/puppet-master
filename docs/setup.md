# Setup

Puppet Master is an MCP server. You need three things: the server itself, a worker model behind an
OpenAI-compatible API, and a registration in your MCP client. An AI agent doing this should follow
[INSTALL_FOR_AGENTS.md](../INSTALL_FOR_AGENTS.md), which has a verify step after each part.

## 1. Install the server

Requirements: Python 3.11+, [`uv`](https://docs.astral.sh/uv/), `git`. Developed and tested on
macOS; Linux is untested.

```bash
git clone https://github.com/khr898/puppet-master.git
cd puppet-master
uv sync
uv run pytest -q
```

## 2. Pick a worker model

The worker is any model served at `POST /v1/chat/completions`. Puppet Master reads three settings:

| Setting | Environment variable | `puppet-master.toml` | Default |
|---|---|---|---|
| Server URL, without `/v1` | `PUPPET_BASE_URL` | `[server] base_url` | `http://127.0.0.1:8000` |
| Model id | `PUPPET_MODEL` | `[server] model` | `flint` |
| API key | `PUPPET_API_KEY` | not supported | none |

Environment variables win over the file. The key is sent as `Authorization: Bearer <key>` and is
read only from the environment, so it cannot be committed by accident.

| Server | Start it | `PUPPET_BASE_URL` | `PUPPET_MODEL` |
|---|---|---|---|
| llama.cpp | `llama-server -m model.gguf --alias my-model --port 8000 -c 16384 --jinja` | `http://127.0.0.1:8000` | the `--alias` |
| Ollama | `ollama serve`, `ollama pull <tag>` | `http://127.0.0.1:11434` | the tag |
| LM Studio | start the local server in the app | `http://127.0.0.1:1234` | the id shown in the app |
| vLLM | `vllm serve <model>` | `http://127.0.0.1:8000` | `<model>` |
| mlx_lm | `mlx_lm.server --model <model>` | `http://127.0.0.1:8080` | `<model>` |
| OpenRouter or another hosted API | get a key from the provider | provider base URL (OpenRouter: `https://openrouter.ai/api`) | provider model id |

Check that it answers:

```bash
curl -s "$PUPPET_BASE_URL/v1/models"
```

**Choosing a model.** Accuracy follows the model: a larger or less-quantized model, or a reasoning
model with thinking on, reads and writes more accurately than a small heavily-quantized one. Pick the
best model your memory allows, then measure it (see [Measure a model](#6-measure-a-model)).

**Things to match to the server:**

- *Context window.* Set `ctx` in `puppet-master.toml` to what the server really gives the model.
  Ollama defaults to a small window; raise it (for example `OLLAMA_CONTEXT_LENGTH=16384`).
- *Structured output.* Requests ask for JSON-schema output. Servers with grammar-constrained decoding
  (llama.cpp, vLLM) give the cleanest results; others still work because replies are validated in
  code and a bad one is retried once.
- *Reasoning models.* Thinking tokens share the context window. Turn thinking off on the server where
  possible, or set `[budget] reasoning_reserve` so chunks leave room for it.
- *Hosted APIs.* Every file the worker reads leaves your machine and each call is billed. Use a local
  server for private code.

## 3. Register the MCP server

Claude Code:

```bash
claude mcp add puppet-master \
  -e PUPPET_BASE_URL=http://127.0.0.1:11434 \
  -e PUPPET_MODEL=your-model \
  -- uv --directory "$(pwd)" run puppet-master
```

Omit the `-e` lines to use the defaults. Add `-e PUPPET_API_KEY=...` for hosted APIs.

Claude Desktop (MCP config JSON; use the absolute path):

```json
"puppet-master": {
  "command": "uv",
  "args": ["--directory", "/path/to/puppet-master", "run", "puppet-master"],
  "env": { "PUPPET_BASE_URL": "http://127.0.0.1:11434", "PUPPET_MODEL": "your-model" }
}
```

Restart the client and start a new session. The tools `puppet_status`, `puppet_read`,
`puppet_write`, `puppet_check`, `puppet_apply`, `puppet_reject`, `puppet_ask` and `puppet_server`
should be listed.

## 4. Configure a project

Create `puppet-master.toml` in the project root and add `.puppet-master/` to its `.gitignore`.

```toml
[check]
default = "test"

[check.commands]            # argument lists, no shell
test = ["uv", "run", "pytest", "-q", "-x"]
lint = ["uv", "run", "ruff", "check", "."]

[server]
ctx = 16384                 # the model's real context window
temperature = 0

[budget]
chunk_tokens = 10000        # file tokens per read chunk; keep well below ctx
focus_tokens = 3000         # tokens a focused read shows the model
region_tokens = 2000        # files above this are edited by region
timeout_s = 90
```

The project's check command must pass on a clean checkout, or every patch will fail its check. The
project must be a git repository.

## 5. Verify

In a new session in the project, call `puppet_status`; it should report the worker up with token
budgets. Then call `puppet_read` on a small file. Afterwards:

```bash
uv run puppet-stats --since 1d
```

## 6. Measure a model

`puppet-eval` runs 20 read questions and 7 write tasks about this repo through the real read and
write code, so claim verification and patch checks count toward the score.

```bash
uv run puppet-eval --label my-model
uv run puppet-eval --compare my-model other-model
uv run puppet-savings --label my-model-savings      # runs the model; estimates Claude tokens saved
uv run puppet-savings --show my-model-savings       # print a saved run
```

Results are saved to `evals/results/<label>.json`.

## Optional: let Puppet Master run llama.cpp for you

With a llama.cpp `llama-server`, Puppet Master can start the server on demand and stop it when idle,
which frees the model's memory between sessions. Claude can only call `start`, `stop` or `restart`;
the flags come from `~/.puppet-master/server.toml`, which you write once:

```toml
binary = "/path/to/llama.cpp/build/bin/llama-server"
model  = "/path/to/model.gguf"
host = "127.0.0.1"          # only 127.0.0.1 is accepted
port = 8000
ctx = 16384
parallel = 1                # one slot gets the whole context
gpu_layers = 99
flash_attn = "on"
cache_type_k = "q8_0"       # 8-bit KV cache: about half the memory of f16
cache_type_v = "q8_0"
cache_reuse = 256           # reuse the cache across calls with a shared prefix
reasoning_budget = 0        # 0 suppresses thinking, -1 is unlimited
slot_save_path = "~/.puppet-master/slots"
autostart = true            # start on the first tool call
idle_stop_min = 15          # stop after 15 idle minutes
min_free_gb = 1.5           # memory headroom required beyond the model file
start_timeout_s = 180
```

Build llama.cpp from [its repository](https://github.com/ggml-org/llama.cpp) (Metal is on by default
on Apple Silicon) and download any GGUF that fits your memory with room for the KV cache. Before
starting, Puppet Master checks free memory and refuses to launch if the model would not fit. It only
ever stops a server it started itself. Without this file, `puppet_server` reports `no_server_config`,
which is expected for every other backend.

## Worked example: M3 Pro MacBook, 18 GB

This is the machine the [results](results.md) were measured on. The same reasoning applies to any
machine: work out how much memory the model may use, then pick the largest model that fits with
room for the KV cache.

- **Budget.** macOS lets the GPU use about two-thirds of unified memory by default, so roughly 12 GB
  of 18 GB. macOS and your other apps need another 4-5 GB. The model file plus KV cache must fit in
  the 12 GB.
- **Choice.** A 40B-A2.5B mixture-of-experts model at 2-bit (11.86 GB) fits tightly and is fast,
  because only 2.5B parameters are active per token. A 12B model at 4-bit (about 8 GB) fits with
  room to spare but is slower per token. Larger or 4-bit versions of the 40B model do not fit.
- **Settings used.** 16,384-token context, one slot, 8-bit KV cache (`q8_0`), flash attention, thinking
  off, temperature 0.

```bash
brew install cmake
git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp
cmake -S ~/llama.cpp -B ~/llama.cpp/build -DCMAKE_BUILD_TYPE=Release
cmake --build ~/llama.cpp/build --config Release -j

mkdir -p ~/models
uvx --from huggingface_hub hf download unsloth/flint-GGUF flint-UD-IQ2_XXS.gguf --local-dir ~/models

~/llama.cpp/build/bin/llama-server -m ~/models/flint-UD-IQ2_XXS.gguf --alias flint \
  --host 127.0.0.1 --port 8000 -c 16384 -np 1 -ngl 99 -fa on \
  --cache-type-k q8_0 --cache-type-v q8_0 --cache-reuse 256 --reasoning-budget 0 --jinja
```

Check it with `curl -s http://127.0.0.1:8000/health` and `curl -s http://127.0.0.1:8000/props`
(`n_ctx` should read `16384`). Close memory-heavy apps first; `memory_pressure -Q` should show
plenty free. If it is too tight, lower `ctx` (for example 12288) and `chunk_tokens` (7000). Raising
the GPU memory limit with `sudo sysctl iogpu.wired_limit_mb=...` is a system change: your call, and
it leaves macOS less headroom.

Then register with the default `PUPPET_MODEL=flint` (the `--alias` above) and no other env vars.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `puppet_status` says the worker is down | Check the server and `PUPPET_BASE_URL`; `curl "$PUPPET_BASE_URL/v1/models"`. |
| `flint_offline` in a result | Timeout, refused connection or unparseable reply. Check the server log; raise `[budget] timeout_s` for slow models. |
| `401` / `403` | Missing or wrong `PUPPET_API_KEY`. |
| `over_budget` | The request exceeds the context. Lower `chunk_tokens`, match `ctx` to the server, or split the request. |
| Every patch fails its check | The project's baseline tests fail; fix them first. |
| Server will not start (managed llama.cpp) | Not enough free memory: close apps or lower `ctx`. |
