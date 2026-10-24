# Flint server setup (M3 Pro, 18GB)

Checked 2026-10-24. Run these yourself, in a normal terminal rather than the Claude sandbox.

## What to download (and why this one)

**`flint-UD-IQ2_XXS.gguf`, 11.86 GB**, from [unsloth/flint-GGUF](https://huggingface.co/unsloth/flint-GGUF).

This Mac has 18GB of unified memory. By default, macOS lets the GPU use only about two-thirds of it (roughly 12GB), and macOS itself, Claude Desktop and your other apps need about 4–5GB on top of that. So:

| Quant | Size | Fits on 18GB? |
|---|---|---|
| UD-IQ1_M | 11.35 GB | Fits, but quality drops too far. Not recommended. |
| **UD-IQ2_XXS** | **11.86 GB** | **Yes. Tight, but it leaves room for a 16K KV cache.** ← use this |
| UD-Q2_K_XL (the original plan) | 13.56 GB | No. It's over the GPU limit and pushes macOS into swap. |
| IQ2_M (bartowski, original plan) | 14.88 GB | No |

You need ~12 GB of free disk space.

## 1. Build llama.cpp from master

Flint support ([#30928](https://github.com/ggml-org/llama.cpp/pull/30928)) and the chat-template fix it needs ([#30929](https://github.com/ggml-org/llama.cpp/pull/30929)) were both merged on 2026-10-21. The latest release, v0.7.0 (2026-10-15), and Homebrew's 0.7.0 are older than that, so don't use them.

```bash
xcode-select --install          # skip if already installed
brew install cmake
git clone https://github.com/ggml-org/llama.cpp ~/Projects/llama.cpp
cd ~/Projects/llama.cpp
git log --oneline -200 | grep -i flint     # should show the Flint commit
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j
```

Metal is enabled by default on Apple Silicon.

If the server later fails to load the model with an "unknown/invalid tensor type" error, the low-bit UD quants need Unsloth's branch, which its Flint guide points to. Build that instead:

```bash
git clone -b iq1-narrow https://github.com/unslothai/llama.cpp ~/Projects/llama.cpp-unsloth
cd ~/Projects/llama.cpp-unsloth && cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build --config Release -j
```

Then point `binary` in `~/.flint/server.toml` at `~/Projects/llama.cpp-unsloth/build/bin/llama-server`.

## 2. Download the model

```bash
mkdir -p ~/models/flint
uvx --from huggingface_hub hf download unsloth/flint-GGUF flint-UD-IQ2_XXS.gguf --local-dir ~/models/flint
```

## 3. Test-run the server once by hand

This step is only a one-time test that the build and model work. After the bridge is built (T3b), you won't run this yourself: the bridge starts llama-server on demand with these same settings and stops it after 15 idle minutes to free memory.

Close heavy apps (browsers with many tabs, Docker) first.

```bash
~/Projects/llama.cpp/build/bin/llama-server \
  -m ~/models/flint/flint-UD-IQ2_XXS.gguf --alias flint \
  --host 127.0.0.1 --port 8000 \
  -c 16384 -np 1 -ngl 99 -fa on \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --cache-reuse 256 --reasoning-budget 0 \
  --jinja
```

- `-np 1`: a single slot, so the full 16K context goes to one request.
- `-fa on`, `q8_0`: flash attention plus an 8-bit KV cache, which roughly halves the KV cache's memory use.
- `--cache-reuse 256`: reuses the KV cache of a shared prompt prefix, so a second question about the same files is fast.
- `--reasoning-budget 0`: Flint is a reasoning model, and its template has no switch to turn thinking off. This flag stops thinking from eating the 16K window. Step 4 checks that it works.
- `--jinja`: uses the model's own chat template (that's what needed #30929).
- `--alias flint`: matches the `"model": "flint"` the bridge sends.

## 4. Check it

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/props | python3 -c "import json,sys;print(json.load(sys.stdin)['default_generation_settings']['n_ctx'])"
curl -s http://127.0.0.1:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"flint","messages":[{"role":"user","content":"Reply with JSON only: {\"ok\": true}"}],"response_format":{"type":"json_object"},"max_tokens":50}'
memory_pressure -Q
```

**Reasoning check.** This verifies that the JSON schema and the reasoning setting work together:

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{"model":"flint","messages":[{"role":"user","content":"Which is larger, 9.11 or 9.9? Answer in JSON."}],"response_format":{"type":"json_schema","json_schema":{"schema":{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"]}}},"max_tokens":200}' | python3 -c "import json,sys; r=json.load(sys.stdin); m=r['choices'][0]['message']; print('content:', m.get('content')); print('reasoning chars:', len(m.get('reasoning_content') or '')); print('usage:', r.get('usage'))"
```

- **Pass:** `content` is valid JSON and the reasoning character count is about 0.
- **Fail:** if thinking still shows up or `content` is empty, restart the server without `--reasoning-budget 0` and run the check again. Note which version gives correct JSON; the bridge's fallback (SPEC → Reasoning control) handles the case where thinking can't be suppressed.

Expected results:
- `/health` returns `{"status":"ok"}`.
- `n_ctx` is `16384`.
- The chat reply contains valid JSON.
- `memory_pressure -Q` shows free memory above roughly 20%.

## If memory is too tight

Symptoms: the startup log says the model doesn't fit in the Metal working set, it runs only a few tokens per second, or `memory_pressure` stays in the warning range.

1. Close more apps and try again.
2. Lower the context: set `ctx = 12288` in `~/.flint/server.toml` and `chunk_tokens = 7000` in the project's `flint.toml`. The bridge reads the real `n_ctx` from `/props`.
3. Only as a last resort, raise the GPU memory limit until the next reboot: `sudo sysctl iogpu.wired_limit_mb=13312`. This is a system setting, so it's your call. It leaves macOS less headroom.

Stop the test server with Ctrl-C once the checks pass.

## 5. Give the bridge its server settings

The bridge reads this file to launch Flint. Claude can only call start, stop or restart; it can never change these flags.

```bash
mkdir -p ~/.flint/slots && cat > ~/.flint/server.toml <<'EOF'
binary = "~/Projects/llama.cpp/build/bin/llama-server"
model  = "~/models/flint/flint-UD-IQ2_XXS.gguf"
host = "127.0.0.1"
port = 8000
ctx = 16384
parallel = 1
gpu_layers = 99
flash_attn = "on"
cache_type_k = "q8_0"
cache_type_v = "q8_0"
cache_reuse = 256
reasoning_budget = 0
slot_save_path = "~/.flint/slots"
autostart = true
idle_stop_min = 15
min_free_gb = 1.5
start_timeout_s = 180
EOF
```

## 6. Register with Claude Desktop

Once the bridge is built (Checkpoint A in `tasks/todo.md`), add this to Claude Desktop's MCP config:

```json
"flint-engine": {
  "command": "uv",
  "args": ["--directory", "/path/to/puppet-master", "run", "flint-bridge"]
}
```
