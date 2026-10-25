# Tasks: Usage Saver

Read [plan.md](plan.md) first (the "Instructions for the builder" section). Run every uv command with `UV_CACHE_DIR=.uv-cache`. Do the tasks in order, using TDD, and commit after each one.

These conventions apply to all tasks:
- **Tool results:** every MCP tool returns a `dict` that has been passed through `report.cap()`.
- **Errors:** errors are returned as data in the form `{"error": "<code>", ...}` and are never raised to the MCP layer.
- **Tokens:** `est_tokens(s) = ceil(len(s) / 4)`.
- **Paths:** every tool's paths are absolute. A relative path gets `{"error": "relative_path", "path": p}`.

---

## T1: Project scaffold, config, git init

**Read:** SPEC → Tech Stack, Commands, Project Structure (`flint.toml` example).

**Do:**
- `git init`, then create `.gitignore` with these entries: `.venv/`, `.uv-cache/`, `.flint/`, `__pycache__/`, `.pytest_cache/`, `.coverage`, `.ruff_cache/`.
- Run `uv init --package --name flint-bridge --python 3.11`. Then edit `pyproject.toml`:
  - dependencies: `mcp>=1.2`, `httpx>=0.27`
  - dev group (`uv add --dev`): `pytest`, `pytest-asyncio`, `pytest-cov`, `respx`, `ruff`
  - `[project.scripts]`: `flint-bridge = "flint_bridge.server:main"`, `flint-stats = "flint_bridge.stats:main"`
  - `[tool.ruff]`: `line-length = 100`, `target-version = "py311"`. `[tool.ruff.lint]`: `select = ["E","F","I","UP","B"]`
  - `[tool.pytest.ini_options]`: `asyncio_mode = "auto"`, `markers = ["live: needs llama-server on :8000"]`, `addopts = "-m 'not live'"`
- Create `src/flint_bridge/config.py`:
  ```python
  @dataclass(frozen=True)
  class Config:
      base_url: str = "http://127.0.0.1:8000"
      ctx: int = 16384
      chunk_tokens: int = 10000
      max_chunks: int = 8
      write_max_tokens: int = 6000
      write_max_files: int = 3
      timeout_s: int = 90
      check_timeout_s: int = 300
      check_default: str = "test"
      check_commands: dict[str, list[str]] = field(default_factory=dict)

  def load_config(root: Path) -> Config  # reads root/"flint.toml" if present, else defaults
  ```
  `flint.toml` keys map as follows:
  - `[server] base_url, ctx`
  - `[budget] chunk_tokens, timeout_s, check_timeout_s`
  - `[check] default` and `[check.commands]` (a table of name → argv list)

  The env var `FLINT_BASE_URL` overrides `base_url`. Validate that every command is a non-empty `list[str]`; anything else raises `ValueError`.

**Acceptance:**
- [ ] `UV_CACHE_DIR=.uv-cache uv sync` succeeds, and `uv run python -c "import flint_bridge"` works.
- [ ] `load_config` returns the defaults when the file is missing, parses the spec's example `flint.toml`, and rejects `test = "pytest"` (a string, not a list).

**Verify:** `uv run pytest -q tests/test_config.py`, then run ruff.

**Files:** `pyproject.toml`, `.gitignore`, `src/flint_bridge/__init__.py`, `src/flint_bridge/config.py`, `tests/test_config.py` · **Size:** S

---

## T2: Report cap, token estimate, call log

**Read:** SPEC → Tools (first paragraph), Call log.

**Do:**
- `src/flint_bridge/report.py`:
  - `est_tokens(s: str) -> int`
  - `cap(d: dict, max_tokens: int = 500) -> dict`, which keeps `json.dumps(result)` at or under `max_tokens * 4` characters.
  - The algorithm repeats these steps until the result fits:
    1. Pop the last item from the longest list value. The candidate lists are `claims`, `failures`, `files` and `notes`.
    2. If there are no list items left, cut the longest string value to half its length and append `"…"`.
  - When anything was cut, it sets `"truncated": True`. It never removes the keys `error`, `patch_id`, `patch_path`, `passed` or `applied`.
- `src/flint_bridge/log.py`:
  - `log_call(root: Path | None, **fields)` appends one JSON line to `root/.flint/log.jsonl`, or to `~/.flint/log.jsonl` when `root` is None, creating the directory if needed.
  - The fields are `ts` (ISO8601 UTC, added automatically), `tool`, `task_key`, `duration_ms`, `report_tokens`, `flint_tokens_in`, `flint_tokens_out`, `outcome`, `stripped` and `retries`. Missing fields are written as `null`.
  - The `outcome` value must be one of: `ok`, `error`, `check_failed`, `staged`, `applied`, `rejected`, `miss`, `offline`, `over_budget`.

**Acceptance:**
- [ ] `cap` leaves a small dict unchanged. A dict with 100 claims gets trimmed to fit and has `truncated: True`. A huge `answer` string gets trimmed. The protected keys are never removed.
- [ ] `log_call` writes valid JSONL, appends instead of overwriting, and rejects an unknown `outcome` with `ValueError`.

**Verify:** `uv run pytest -q tests/test_report.py tests/test_log.py`

**Files:** `report.py`, `log.py`, and both test files · **Size:** S

---

## T3: LLM client, `flint_status`, server instructions, MCP round trip

**Read:** SPEC → Tools (`flint_status`), Fallbacks, Policy layers (item 1), Context budget, Commands (MCP config).

**Do:**
- `src/flint_bridge/llm.py`:
  ```python
  class FlintOffline(Exception): ...
  @dataclass
  class Completion: text: str; tokens_in: int; tokens_out: int
  async def complete(cfg: Config, system: str, user: str, *, max_tokens: int, schema: dict | None = None) -> Completion
  ```
  - It POSTs `{base_url}/v1/chat/completions` with `{"model": "flint", "messages": [...], "temperature": 0.2, "max_tokens": ..., "cache_prompt": true, "response_format": {"type": "json_schema", "json_schema": {"schema": schema}}}`, leaving `response_format` out when `schema` is None. The schema makes llama-server grammar-constrain the output, so its JSON structure is always valid. `cache_prompt: true` is always sent, so the KV cache of a shared prompt prefix gets reused.
  - Token counts come from `usage.prompt_tokens` and `usage.completion_tokens`, falling back to `est_tokens`.
  - **Reasoning:** `Completion` also has `reasoning_tokens: int`, taken from `usage.completion_tokens_details.reasoning_tokens`, or estimated from the removed text. `text` is `message.content` with any `<think>…</think>` removed. `message.reasoning_content` is never returned to the caller.
  - It uses `httpx.AsyncClient(timeout=cfg.timeout_s)`. Any `httpx.TransportError`, timeout, or non-2xx response raises `FlintOffline`.
  - `async def health(cfg) -> dict` sends `GET /health` and `GET /props`, and returns `{up, latency_ms, ctx}`. `ctx` comes from `props["default_generation_settings"]["n_ctx"]`, falling back to `cfg.ctx`. Any failure returns `{up: False}`.
- `src/flint_bridge/sysinfo.py`: `mem_pressure() -> int | None` runs `["memory_pressure", "-Q"]` and parses `free percentage: (\d+)%`. It returns `100 - free`, or `None` if anything fails.
- `src/flint_bridge/server.py`:
  - `mcp = FastMCP("flint-engine", instructions=INSTRUCTIONS)` and `def main(): mcp.run()` (stdio).
  - `INSTRUCTIONS` is this exact text:
    > You have a local model, Flint (16K context), via these tools. Before doing bulk work yourself, decide if Flint can do it or a piece of it. Delegate: reading/summarizing big files or logs, boilerplate, scaffolding, tests from a spec, docstrings, mechanical refactors, drafts. Keep: planning, architecture, subtle bugs, security-sensitive code, final review, anything under ~200 output tokens. When you skip Flint, say why in one line. Size every request to Flint's budget (call flint_status once per session): reads ≤10k tokens per chunk, ≤8 chunks; writes ≤3 files, ≤6k tokens total. Pass specific files or path:start-end ranges. Checks are mandatory: act only on verified claims and read the cited lines before acting; read every staged patch before flint_apply; never apply Flint changes any other way. Before compiling or any other memory-heavy command (builds, large test runs, Docker), call flint_server("stop") to give Flint's ~12GB back; the next Flint call restarts it. If Flint is offline or fails twice, do it yourself and tell the user.
  - `flint_status()` returns `{up, model: "flint", ctx, latency_ms, mem_pressure, budgets: {chunk_tokens, max_chunks, write_max_tokens, write_max_files, timeout_s}}`. It is logged with `root=None`.
  - Its tool description: *"Health and budgets of the local Flint model. Call once per session before delegating."*
- `README.md` covers the two ways to register the server:
  - Claude Desktop MCP config JSON, the snippet from SPEC → Commands.
  - `claude mcp add flint-engine -- uv --directory /path/to/puppet-master run flint-bridge`

**Acceptance:**
- [ ] Tests (respx) cover:
  - `complete` parses content and usage.
  - It sends a `json_schema` `response_format` only when a schema is given, and always sends `cache_prompt: true`.
  - A reply with `<think>x</think>{"a":1}` gives `text == '{"a":1}'` and `reasoning_tokens > 0`.
  - A timeout and a 500 response both raise `FlintOffline`.
  - `health` reports `up: False` when it can't connect.
- [ ] `flint_status` returns the budgets even when Flint is down.
- [ ] `uv run flint-bridge` starts and waits on stdio. Check with `timeout 3 uv run flint-bridge < /dev/null`, which must exit without a traceback.

**Verify:** `uv run pytest -q tests/test_llm.py tests/test_status.py`

**Files:** `llm.py`, `sysinfo.py`, `server.py`, `README.md`, and tests · **Size:** M

---

## T3b: Server lifecycle (`flint_server`, autostart, idle stop)

**Read:** SPEC → Server lifecycle, KV cache use, Boundaries (Never).

**Do:** `src/flint_bridge/lifecycle.py`:
```python
@dataclass(frozen=True)
class ServerConfig:  # fields and defaults exactly as the SPEC server.toml example
    binary: Path; model: Path; host: str = "127.0.0.1"; port: int = 8000; ctx: int = 16384
    parallel: int = 1; gpu_layers: int = 99; flash_attn: str = "on"
    cache_type_k: str = "q8_0"; cache_type_v: str = "q8_0"; cache_reuse: int = 256
    reasoning_budget: int = 0
    slot_save_path: Path = Path("~/.flint/slots"); autostart: bool = True
    idle_stop_min: int = 15; min_free_gb: float = 1.5; start_timeout_s: int = 180

def load_server_config(path: Path = Path("~/.flint/server.toml")) -> ServerConfig | None
def build_argv(sc: ServerConfig) -> list[str]
def free_gb() -> float | None          # memory_pressure -Q free% × sysctl hw.memsize
def preflight(sc) -> dict | None       # None = ok, else insufficient_memory error dict
async def start(sc, cfg) -> dict
def stop(sc) -> dict
class IdleWatcher: touch(); async run(sc)   # stops a managed server after idle_stop_min
```
- **`load_server_config`** expands `~`. It returns `None` when the file is missing; tools then return `{"error":"no_server_config","hint":"see docs/setup/flint-server.md"}`.
  - It raises `ValueError` if `host != "127.0.0.1"`, if `binary` or `model` doesn't exist, or if `cache_type_*` isn't one of `f16`, `q8_0`, `q4_0`.
- **`build_argv`** returns exactly:
  `[binary, "-m", model, "--alias", "flint", "--host", host, "--port", str(port), "-c", str(ctx), "-np", str(parallel), "-ngl", str(gpu_layers), "-fa", flash_attn, "--cache-type-k", k, "--cache-type-v", v, "--cache-reuse", str(cache_reuse), "--reasoning-budget", str(reasoning_budget), "--slot-save-path", str(slot_save_path), "--jinja"]`
  - No tool takes flags or paths for llama-server.
- **`preflight`:** `need = model.stat().st_size/1e9 + kv_gb + sc.min_free_gb`, where `kv_gb = 1.0` for q8_0 and 2.0 for f16 at 16K (scaled linearly by `ctx/16384`).
  - If `free_gb()` is less than `need`, it returns `{"error":"insufficient_memory","need_gb","free_gb","hint":"close apps or lower ctx in ~/.flint/server.toml"}`.
  - If `free_gb()` returns None, it skips the check.
- **`start`:**
  1. Take an exclusive `fcntl.flock` on `~/.flint/server.lock`.
  2. If `llm.health(cfg)` reports up, return `{up: true, managed: <pidfile exists and pid alive>}`.
  3. Otherwise run `preflight`, then `subprocess.Popen(argv, stdout=log, stderr=STDOUT, start_new_session=True)`, with the log at `~/.flint/server.log`.
  4. Write `~/.flint/server.pid` as JSON `{pid, argv0, started}`.
  5. Poll `/health` every 1s up to `start_timeout_s`. On timeout, kill the process and return `{"error":"start_timeout","log_tail": last 10 lines}`.
- **`stop`:** reads the pidfile. It only signals the process if it's alive and its command (`ps -o command= -p <pid>`) contains both `sc.binary.name` and `sc.model.name`. It sends SIGTERM, waits up to 10s, then SIGKILL, and removes the pidfile. With no pidfile, or a mismatch, it returns `{"stopped": false, "reason": "not_managed"}`.
- **Autostart:** in `server.py`, every Flint-calling tool (read, write, ask) calls `ensure_up()` first. If the server is down and `autostart` is on, it runs `start`, so startup time doesn't count toward the request timeout. If `start` fails, the tool returns that error. After any call, `IdleWatcher.touch()`.
- **IdleWatcher:** started with the FastMCP server's lifespan. Every 60s it checks whether `now - last_touch > idle_stop_min` and the server is managed, and if so calls `stop`.
- **`flint_server(action: Literal["start","stop","restart"])`:** returns `{up, managed, pid, rss_gb (ps -o rss=), mem_free_pct}`.
  - Its description: *"Start/stop/restart the local Flint llama-server using the fixed settings in ~/.flint/server.toml. Usually unnecessary: Flint tools autostart it, and it stops itself after idle_stop_min to free memory. Use stop when the user needs RAM back, and always before compiling or other memory-heavy commands."*
- Add `managed`, `pid` and `autostart` to the `flint_status` output.

**Acceptance:**
- [ ] `build_argv` output matches the exact list. `host="0.0.0.0"` raises `ValueError`.
- [ ] `preflight` with a monkeypatched `free_gb=10` and a 12GB fake model (a sparse file made with `f.truncate`) returns `insufficient_memory`, and `Popen` is never called.
- [ ] `start` against a fake "server" works: point `binary` at `tests/fixtures/fake_server.py`, a tiny `http.server` that answers `/health` and `/props` and has a shebang and `chmod +x`. The pidfile gets written, and `stop` terminates it.
- [ ] `stop` refuses a pidfile whose pid belongs to an unrelated process (use `os.getpid()` of the test runner, which won't match).
- [ ] If the port is already serving, `start` returns `managed: false` and `stop` doesn't kill it.
- [ ] `IdleWatcher` with `idle_stop_min=0` and a fake clock calls `stop` once.
- [ ] Every test uses `tmp_path` for `~/.flint` (monkeypatch `HOME`), so nothing touches the real home directory.

**Verify:** `uv run pytest -q tests/test_lifecycle.py`

**Files:** `lifecycle.py`, `server.py`, `tests/test_lifecycle.py`, `tests/fixtures/fake_server.py` · **Size:** M

### ⛳ Checkpoint A: stop and report to the user
- [ ] All tests and ruff pass.
- [ ] **The user** creates `~/.flint/server.toml` from the SPEC example (paths from `docs/setup/flint-server.md`), registers `flint-engine` in Claude Desktop, and opens a new Code-tab session.
  - The tools appear.
  - `flint_server("start")` brings Flint up, `flint_status` shows `up: true, managed: true`, and `flint_server("stop")` frees the memory.
- [ ] **Reasoning check** (the commands are in `docs/setup/flint-server.md` step 4). With `reasoning_budget = 0`, a `json_schema` request returns valid JSON in `content`, and `reasoning_tokens` is about 0.
  - If thinking still appears, or the JSON breaks, switch to the SPEC → Reasoning control fallback.
  - Record the result in ADR 0001's Consequences.
- [ ] Don't continue until the user confirms the tool is visible.

---

## T4: Line numbering and claim verification, with adversarial fixtures

**Read:** SPEC → Read verification (steps 1, 4, 5), Code Style.

**Do:** `src/flint_bridge/read.py` (pure functions only in this task):
```python
@dataclass(frozen=True)
class Claim: text: str; file: str; line: int; quote: str

def number_lines(lines: list[str], start: int = 1) -> str   # f"{n:>5}| {line}" joined by "\n"
def _norm(s: str) -> str                                      # " ".join(s.split())
def verify_claim(claim: Claim, files: dict[str, list[str]], window: int = 2) -> bool
def parse_claims(raw: list) -> tuple[list[Claim], int]        # (valid, malformed_count)
def filter_claims(claims, files) -> tuple[list[Claim], int]   # (verified, stripped)
```
- `verify_claim` returns False when:
  - the file is not in `files`,
  - the line is < 1 or > len,
  - the quote is empty after `_norm`,
  - `_norm(quote)` is not a substring of `_norm(" ".join(lines[lo:hi]))`, where `lo = max(0, line-1-window)` and `hi = min(len, line+window)`.
- `parse_claims` drops entries that aren't dicts with the right types (`line` must be an int, or a numeric string that gets coerced). Dropped entries count as malformed, and malformed entries count toward `stripped`.
- `tests/fixtures/hallucinations.py` has one source file (about 40 lines of Python) and at least 10 bad claims. Each fails the check for one of these reasons:
  - made-up function name
  - wrong file
  - line off by 5
  - quote paraphrased instead of exact
  - empty quote
  - line 0
  - line past EOF
  - quote that spans more than the window
  - `line` given as a non-numeric string
  - missing `quote` key

  The fixture also has 3 good claims: an exact match, a quote with extra whitespace, and a quote on line ±2.

**Acceptance:**
- [ ] All 10+ bad claims are removed and all 3 good ones are kept, by a test that loops over the fixture.
- [ ] `read.py` has 100% branch coverage in this task.

**Verify:** `uv run pytest -q tests/test_verify.py --cov=flint_bridge.read --cov-branch`

**Files:** `read.py`, `tests/fixtures/hallucinations.py`, `tests/test_verify.py` · **Size:** S

---

## T5: `flint_read` single chunk, with budget preflight and line ranges

**Read:** SPEC → Read verification, Context budget, Fallbacks.

**Do:**
- `src/flint_bridge/budget.py`:
  - `parse_path(p: str) -> tuple[Path, int | None, int | None]` accepts `path` or `path:START-END`. The range only counts as a range when the suffix matches `:\d+-\d+$`.
  - `expand(paths) -> list[...]` expands globs with `glob.glob(recursive=True)`. Directories are rejected with `{"error": "directory_not_allowed"}`.
  - `fits(prompt_tokens, max_out, cfg) -> bool` checks `prompt_tokens + max_out <= cfg.ctx - 512`.
- In `read.py`, add `async def flint_read_impl(cfg, paths, question) -> dict`:
  1. Resolve the paths and read the files as UTF-8 (on failure, `{"error":"unreadable","path":p}`). Slice to the range, keeping the original line numbers.
  2. Build `files: dict[str, list[str]]`, keyed by absolute path, holding only the loaded slice. The index offset maps line N to `lines[N - start]`, so store `(start, lines)` and adjust `verify_claim` to accept an offset. Update the T4 tests to match.
  3. Estimate the file tokens. If they exceed `cfg.chunk_tokens * cfg.max_chunks`, return `{"error":"over_budget","est_tokens":n,"limit":...,"hint":"narrow paths or use path:start-end"}`. Single chunk only in this task: if they exceed `chunk_tokens`, return the same error for now (T6 removes this).
  4. Call `complete(schema=READ_SCHEMA, max_tokens=1500)` (a JSON Schema constant: object with required `answer` string and `claims` array of objects with required `text` string, `file` string, `line` integer, `quote` string) with the prompt below.
  5. Parse the JSON. If it's invalid or missing `answer`, retry once with `"Your last reply was not valid JSON. Reply with JSON only."` appended. If it fails again, return `{"error":"unverifiable_output"}`.
  6. Filter the claims and return `{"answer", "claims":[{text,file,line}], "stripped", "chunks":1}`. When `stripped > 0`, also return `"answer_confidence":"degraded"`. Quotes are not returned, to save tokens.
- Prompts (constants in `read.py`):
  - **SYSTEM_READ:** `"You answer questions about source files. Every factual statement must be backed by a claim citing the exact file path, the line number shown in the left margin, and a short quote copied character-for-character from that line. Never cite lines you were not shown. Reply with JSON only: {\"answer\": string, \"claims\": [{\"text\": string, \"file\": string, \"line\": int, \"quote\": string}]}."`
  - **User message:** `"".join(f"=== FILE {path} ===\n{number_lines(lines, start)}\n" for each file in sorted path order) + f"\nQUESTION: {question}"`. Files come first and the question last, so a follow-up question about the same files reuses the KV cache.
- `server.py`: register `flint_read(paths: list[str], question: str)`.
  - It catches `FlintOffline` and returns `{"error":"flint_offline"}`.
  - It times the call, then logs `tool, duration_ms, report_tokens, flint_tokens_in/out, outcome, stripped`, using the root of the first path from `gitops`, or that path's parent if it isn't in a repo.
  - Its description: *"USE THIS FIRST to read, search or summarize files/logs instead of opening them yourself. Returns a short answer whose claims are mechanically verified against the source (unverified claims are removed). Before acting on a claim, read its cited lines yourself. Limits: ≤10k tokens of file per chunk, ≤8 chunks; pass specific files or path:start-end ranges, never directories. Not for drafting prose (use flint_ask)."*

**Acceptance:**
- [ ] With the mocked LLM returning the hallucination fixture, the report has only verified claims and `stripped` equals the number of bad claims.
- [ ] Invalid JSON twice gives `unverifiable_output`. Invalid JSON once, then valid, succeeds.
- [ ] `src/x.py:10-20` sends only lines 10–20, numbered 10–20, and a claim on line 15 verifies.
- [ ] A directory path gives `directory_not_allowed`, a relative path gives `relative_path`, and too much input gives `over_budget` without any LLM call (respx asserts zero calls).
- [ ] The report always fits within 500 tokens.

**Verify:** `uv run pytest -q tests/test_read.py tests/test_budget.py`

**Files:** `budget.py`, `read.py`, `server.py`, and tests · **Size:** M

---

## T6: Multi-chunk map-reduce

**Read:** SPEC → Read verification (step 2), Context budget.

**Do:** in `read.py`:
- `chunk_files(files, chunk_tokens) -> list[dict[str, tuple[int, list[str]]]]` packs files into chunks in order. A file bigger than one chunk is split on line boundaries, and each piece keeps its true starting line number.
- If there are more than `cfg.max_chunks` chunks, return `over_budget`.
- **Map:** for each chunk, run the T5 single-chunk flow (prompt, parse, retry, filter) and collect the verified claims. Each verified claim gets an id like `c1`, `c2`, and so on.
- If there's exactly one chunk, return as in T5. Otherwise run the **merge** step:
  - SYSTEM_MERGE: `"Combine partial answers into one answer to the question. You may only rely on the listed claims. Reply with JSON only: {\"answer\": string, \"claim_ids\": [string]}."`
  - User message: the question, the partial answers, and the claims as `id: text (file:line)`.
  - Unknown ids in `claim_ids` are dropped. The final claims are the ones the merge selected. `stripped` is the sum across chunks. If the merge output is invalid twice, return `{"error":"unverifiable_output"}`.
- Remove T5's single-chunk-only restriction.

**Acceptance:**
- [ ] Two files that each need their own chunk make 3 LLM calls (2 map, 1 merge).
- [ ] A merge reply that cites id `c99` (which doesn't exist) never shows up in the report.
- [ ] A 2,500-line file is split, a claim on line 2,400 verifies against the right slice, and 9 chunks gives `over_budget`.

**Verify:** `uv run pytest -q tests/test_read.py`

**Files:** `read.py`, `tests/test_read.py` · **Size:** S

### ⛳ Checkpoint B: stop and report to the user
- [ ] `uv run pytest --cov=flint_bridge.read --cov=flint_bridge.report --cov-report=term-missing` shows ≥ 90% coverage for both.
- [ ] Summarize the adversarial results for the user. Every fabricated claim is removed.

---

## T7: Git helpers and `flint_check` (allow-list, failure extraction)

**Read:** SPEC → Tools (`flint_check`), Boundaries.

**Do:**
- `src/flint_bridge/gitops.py`. Every call uses `subprocess.run(argv, cwd=..., capture_output=True, text=True, timeout=...)` and never `shell=True`.
  - `git_root(path: Path) -> Path | None` runs `git rev-parse --show-toplevel`.
  - `run_git(root, *args, input: str | None = None) -> str` raises `GitError` on a non-zero exit.
- `src/flint_bridge/check.py`:
  - `extract_failures(output: str) -> list[str]` collects lines matching `^(FAILED|ERROR) `, plus lines matching `^\S+:\d+:\d+: ` (ruff and mypy style). If there are none and the exit code is non-zero, it uses the last 20 lines. The result is at most 20 items of at most 200 characters each.
  - `run_check(cfg, cwd: Path, name: str) -> dict`:
    - If `name not in cfg.check_commands`, it returns `{"error":"not_allowed","allowed":[...]}`.
    - Otherwise it runs the argv list with `timeout=cfg.check_timeout_s` and returns `{passed: rc == 0, exit_code, failures}`.
    - On timeout it returns `{"passed": False, "exit_code": None, "failures": ["timeout"]}`.
- `server.py`: register `flint_check(cmd: str, root: str, patch_id: str | None = None)`.
  - In this task it only supports `patch_id=None` and runs in `root`. With a `patch_id` it returns `{"error":"not_implemented"}`, which T8 replaces.
  - `cmd` is a **name** from the allow-list. It is never parsed as a command line.
  - Its description: *"Run an allow-listed check (names from flint.toml, e.g. 'test', 'lint') and get only the failures. With patch_id, runs against that staged patch in an isolated worktree."*

**Acceptance:**
- [ ] `cmd="rm -rf /"` and `cmd="test; ls"` both give `not_allowed`, and nothing runs.
- [ ] A passing pytest returns `passed: True, failures: []`. A failing pytest returns the `FAILED ...` lines, at most 20.
- [ ] No `shell=True` anywhere. A test asserts this with `grep -rn "shell=True" src` finding nothing.

**Verify:** `uv run pytest -q tests/test_check.py`. The tests build a small project in `tmp_path` with a passing and a failing pytest.

**Files:** `gitops.py`, `check.py`, `server.py`, and tests · **Size:** M

---

## T8: Worktree sandbox: baseline, symlinks, run the check

**Read:** plan.md → Architecture Decision 3; SPEC → Write pipeline (step 3).

**Do:** in `gitops.py`, add a context manager:
```python
@contextmanager
def baseline_worktree(root: Path) -> Iterator[Path]:
```
It does the following, in order:
1. `tmp = Path(tempfile.mkdtemp(prefix="flint-wt-"))`, then `git worktree add --detach <tmp> HEAD`.
2. `diff = run_git(root, "diff", "HEAD", "--binary")`. If the diff isn't empty, run `run_git(tmp, "apply", "--whitespace=nowarn", input=diff)`.
3. For each file in `git ls-files --others --exclude-standard` (in `root`), copy it to `tmp` with the same relative path.
4. For each name in `(".venv", "node_modules")` that exists in `root` and not in `tmp`, run `os.symlink(root/name, tmp/name)`.
5. `run_git(tmp, "add", "-A")`. The index is now the user's current state.
6. `yield tmp`. In the `finally` block, run `git worktree remove --force <tmp>` and then `shutil.rmtree(tmp, ignore_errors=True)`.

Then add `stage_and_check(cfg, root, new_files: dict[Path, str], check_name) -> dict` (used by T9):
- Inside `baseline_worktree`, it writes each file's new content to `tmp/<relative path>`, creating parent directories.
- It takes `patch = run_git(tmp, "diff", "--binary")`, the changes against the index. An empty patch returns `{"error":"no_changes"}`.
- It runs `run_check(cfg, tmp, check_name)` and returns `{patch, diffstat: run_git(tmp, "diff", "--stat"), check}`.

Replace the T7 `not_implemented` path: `flint_check(cmd, root, patch_id)` loads the staged patch (see T9's metadata), applies it in a `baseline_worktree`, runs `cmd`, and returns the result.

**Acceptance:**
- [ ] Integration test in a `tmp_path` git repo, with one commit, one uncommitted edit, and one untracked file:
  - `baseline_worktree` contains all three states.
  - The real tree's `git status --porcelain` is identical before and after.
  - The worktree is removed afterwards (`git worktree list` shows one entry).
- [ ] `.venv` is symlinked when it exists in root.
- [ ] `stage_and_check` with a change that breaks a test returns `check.passed: False` and a non-empty patch, and the real tree is untouched.
- [ ] Cleanup still happens when the check raises (test with a monkeypatched `run_check` that raises).

**Verify:** `uv run pytest -q tests/test_worktree.py`

**Files:** `gitops.py`, `check.py`, `server.py`, `tests/test_worktree.py` · **Size:** M

---

## T9: `flint_write` (budget, generation, staged patch, auto-check)

**Read:** SPEC → Write pipeline (steps 1–4), Context budget, Open Questions (resolved #2).

**Do:** `src/flint_bridge/write.py`:
- `task_key(task: str) -> str` returns `sha256(_norm(task).lower())[:12]`.
- `async def flint_write_impl(cfg, task, files, constraints) -> dict`:
  1. `files` is a list of absolute paths, at most `cfg.write_max_files` (otherwise `over_budget`). Paths that don't exist count as new files. All of them must share one `git_root` (otherwise `{"error":"not_in_repo"}` or `{"error":"multiple_repos"}`).
  2. Budget: total `est_tokens` of the current contents must be ≤ `cfg.write_max_tokens`, and so must every single file. Otherwise return `over_budget` with the hint `"split into one task per file; files over ~6k tokens stay with Claude"`. Also call `fits(prompt_tokens, max_out=cfg.write_max_tokens + 500, cfg)`.
  3. Check attempts (see T10's `attempts.give_up(root, key)`). If it says give up, return `{"give_up": true, "task_key": k}` and log `outcome="miss"`.
  4. Prompt (constants):
     - SYSTEM_WRITE: `"You edit source files. Return the COMPLETE new content of every file you change — never diffs or fragments. Do not change files you were not given. Reply with JSON only: {\"files\": [{\"path\": string, \"content\": string}], \"notes\": string}."`
     - User message: first `=== FILE <abs path> ===` followed by the raw content (no line numbers) for each file in sorted order, or `(new file)`; then `TASK` and `CONSTRAINTS` (or "none") **last**, so retries reuse the KV cache of the file prefix.
  5. `complete(schema=WRITE_SCHEMA, max_tokens=cfg.write_max_tokens + 500)` (`files`: array of {`path`, `content`} strings; `notes`: string). Use the same JSON retry once as T5. Reject any returned path not in `files` with `{"error":"unexpected_path"}`.
  6. `stage_and_check(cfg, root, new_files, cfg.check_default)`.
  7. Save `patch` to `root/.flint/staging/<id>.patch`, where `id = uuid4().hex[:12]`. Save `<id>.json` alongside it, containing `{patch_id, task_key, root, files, sha256, check, created, applied: false}`.
  8. `attempts.record(root, key, passed=check["passed"])`.
  9. Return `{patch_id, patch_path, diffstat, notes (≤300 chars), check: {passed, failures}}` and log `outcome="staged"` or `"check_failed"`.
- `server.py`: register `flint_write(task: str, files: list[str], constraints: str = "")`.
  - Its description: *"USE THIS for boilerplate, scaffolding, tests from a spec, docstrings, mechanical refactors. Flint writes a STAGED patch (your working tree is untouched) and tests run on it automatically. You MUST read the patch at patch_path before calling flint_apply. To retry, call again with the same task and your feedback in constraints (max 2 attempts). Limits: ≤3 files, ≤6k tokens total; larger files stay with you."*

**Acceptance:**
- [ ] With a mocked LLM returning a valid edit, the patch file and metadata exist, `sha256` matches the patch file, the real tree is unchanged, and `check.passed` reflects the test.
- [ ] A reply containing a path outside `files` gives `unexpected_path`, and nothing is staged.
- [ ] A 30 KB file gives `over_budget` with no LLM call, and 4 files gives `over_budget`.
- [ ] A new (missing) file path produces a patch that creates it.

**Verify:** `uv run pytest -q tests/test_write.py`

**Files:** `write.py`, `server.py`, `tests/test_write.py` (plus the `attempts.py` stub from T10, if TDD-ing in order; otherwise create it here) · **Size:** M

---

## T10: `flint_apply` and attempt tracking / miss

**Read:** SPEC → Write pipeline (steps 5–6), Boundaries (Never).

**Do:**
- `src/flint_bridge/attempts.py` keeps a JSON dict `{task_key: {"failed": int}}` in `root/.flint/attempts.json`:
  - `record(root, key, passed)` increments `failed` when the attempt didn't pass.
  - `give_up(root, key) -> bool` returns `failed >= 2`.
  - `reset(root, key)`.
- `write.py` gets `def flint_apply_impl(root, patch_id) -> dict`, which refuses with these errors:
  - `{"error":"unknown_patch"}` when the metadata is missing
  - `{"error":"already_applied"}`
  - `{"error":"check_failed"}` when `check.passed` is false
  - `{"error":"patch_modified"}` when the sha256 of the patch file ≠ the metadata
  - `{"error":"does_not_apply","detail":<first 300 chars>}` when `git apply --check` fails in `root`

  Otherwise it runs `git apply` in `root`, sets `applied: true` in the metadata, calls `attempts.reset`, logs `outcome="applied"`, and returns `{applied: true, files: [...]}`.
- `server.py`: register `flint_apply(root: str, patch_id: str)`.
  - Its description: *"Apply a staged Flint patch to the working tree. Only call after you have read the patch file yourself and agree with it. Refuses if checks failed or the patch was modified."*
- Add `flint_reject(root: str, patch_id: str, reason: str)`. It records a failed attempt, logs `outcome="rejected"`, and deletes the staged files. This keeps the attempt count honest when Claude rejects a patch that passed its checks.
  - Its description: *"Reject a staged patch after review; counts as a failed attempt."* (It's already in the SPEC tools table.)

**Acceptance:**
- [ ] Each refusal case has its own test, and `flint_apply` never changes the tree in any of them (porcelain status compared).
- [ ] The happy path applies cleanly, and running it again gives `already_applied`.
- [ ] Two failed attempts (`check_failed` or `rejected`) on the same task: the third `flint_write` returns `give_up` without calling the LLM, and the log has `outcome="miss"`.

**Verify:** `uv run pytest -q tests/test_apply.py tests/test_attempts.py`

**Files:** `attempts.py`, `write.py`, `server.py`, and tests · **Size:** M

### ⛳ Checkpoint C: stop and report to the user
- [ ] `uv run pytest --cov=flint_bridge --cov-report=term-missing` shows ≥ 90% on `read.py`, `write.py`, `check.py`, `report.py`.
- [ ] Walk the user through the refusal-test list. A patch can't reach the tree without a passed check and an unmodified hash.

---

## T11: `flint_ask` and the `flint-stats` CLI

**Read:** SPEC → Tools (`flint_ask`), Open Questions (resolved #3), Call log.

**Do:**
- `flint_ask(prompt: str, context: str = "")`:
  - It uses `complete(max_tokens=1500)` with the system prompt `"Write the requested draft. Be concise."`.
  - It returns `{"draft": text, "verified": false}`, preflights with `fits` (`over_budget` when it doesn't fit), and is logged with `root=None`.
  - Its description: *"Drafts and prose only (docs, emails, commit messages, rewrites, explanations of text you provide). Output is UNVERIFIED. For any question about code or files, use flint_read instead."*
- `src/flint_bridge/stats.py`: `main()` parses `--log PATH` (default `./.flint/log.jsonl`) and `--since 7d|24h`, then prints:
  - calls per tool,
  - outcome counts,
  - accept rate (`applied / (applied + rejected + miss + check_failed)`),
  - average `report_tokens`,
  - total `flint_tokens_in` and `flint_tokens_out`,
  - average `duration_ms` per tool.

  It uses plain text output and no new dependencies.

**Acceptance:**
- [ ] `flint_ask` output always has `verified: false`, and its description mentions `flint_read`.
- [ ] `flint-stats` on a fixture log with known numbers prints the expected accept rate and averages, and `--since` filters correctly.

**Verify:** `uv run pytest -q tests/test_ask.py tests/test_stats.py`

**Files:** `server.py`, `stats.py`, and tests · **Size:** S

---

## T11b: Sample capture, reasoning budget in config

**Read:** SPEC → Sample capture, Reasoning control; ADR 0002.

**Do:**
- `config.py`: add `samples: bool = True` and `reasoning_reserve: int = 0` (`[budget] reasoning_reserve`, `[log] samples`). Use `effective_chunk_tokens = chunk_tokens - reasoning_reserve` everywhere chunk sizing happens, and add `reasoning_reserve` to every `max_tokens`.
- `src/flint_bridge/samples.py`:
  - `record(root, cfg, *, tool, system, user, raw_output, verdict)` appends to `root/.flint/samples/YYYY-MM-DD.jsonl` when `cfg.samples` is on. After each write, if the directory is over 200MB, it deletes the oldest files until it's under.
  - Called from `flint_read` (verdict `verified` or `stripped:N` or `unverifiable`), `flint_write` (`check_failed` or `staged`), `flint_apply` (updates the matching sample's verdict to `applied` by appending `{patch_id, verdict}`), and `flint_reject` (`rejected`).
- `log.py`: add a `reasoning_tokens` field. `flint-stats` prints the average `reasoning_tokens` per tool.

**Acceptance:**
- [ ] With `samples=false`, nothing is written. With it on, one line is written per Flint call, containing the prompt and the raw output.
- [ ] The 200MB cap removes the oldest file first (test with a tiny cap that's monkeypatched in).
- [ ] With `reasoning_reserve=3000`, chunks shrink by 3000 tokens and `max_tokens` grows by 3000.

**Verify:** `uv run pytest -q tests/test_samples.py tests/test_config.py`

**Files:** `config.py`, `samples.py`, `log.py`, `stats.py`, `server.py` · **Size:** S

---

## T12: Final tool descriptions, this project's `CLAUDE.md`, live smoke tests

**Read:** SPEC → Policy layers, Success Criteria; `docs/spec/claude-md-snippet.md`.

**Do:**
- Test: every tool's description contains its limit text and the "mandatory check" wording given in its task above.
- Create `flint.toml` in this repo:
  - `test = ["uv","run","pytest","-q","-x"]`
  - `lint = ["uv","run","ruff","check","."]`
  - `default = "test"`
- Create `CLAUDE.md` at the repo root with two parts:
  - A short project section: the commands from SPEC → Commands, the `UV_CACHE_DIR=.uv-cache` note, and a link to `docs/spec/SPEC.md`.
  - The `## Flint delegation` section copied from `docs/spec/claude-md-snippet.md`.
- `tests/live/test_live.py` (marker `live`): one real call each to `flint_status` (`up: true`), `flint_read` on `src/flint_bridge/report.py` asking "what does cap() protect from removal?", `flint_ask`, and `flint_write` on a temp git repo adding a docstring. Each asserts the report's shape and that it fits within 500 tokens. It skips automatically when `/health` can't be reached.
- Update `README.md` with a usage and trial section: how to run `flint-stats --since 7d`.

**Acceptance:**
- [ ] The default suite passes, and `uv run pytest -m live` skips cleanly when Flint is down.
- [ ] `CLAUDE.md` and `flint.toml` exist, and their check commands match.

**Verify:** `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`

**Files:** `flint.toml`, `CLAUDE.md`, `README.md`, `tests/live/test_live.py`, `tests/test_descriptions.py` · **Size:** S

---

## T13: `flint-eval` bake-off harness (picks the model; ADR 0001)

**Read:** ADR 0001; SPEC → Success Criteria.

**Do:** `src/flint_bridge/eval.py` with the script `flint-eval = "flint_bridge.eval:main"`.
- Task set in `evals/`, committed to the repo:
  - **10 read tasks** about this repo's own source. Each has `{paths, question, must_mention: [str]}`; the answer must contain every keyword, case-insensitive.
  - **5 write tasks**, each in a throwaway git repo copied from `evals/fixtures/<name>/` with a failing test that the task should fix: `{task, files, fixture}`.
- `flint-eval --label <name>` runs every task through the real `*_impl` functions against the running server. It writes `evals/results/<label>.json` and prints a table of:
  - read: answer-correct %, average claims kept, average stripped, `unverifiable` count
  - write: check-pass %
  - speed: tok/s (`flint_tokens_out / duration`) and average `reasoning_tokens`
  - memory: peak llama-server RSS (polled with `ps -o rss=` every 1s)
- `flint-eval --compare a b` prints the two results side by side.
- The tests use the fake server from T3b. They check that the harness scores correctly, not that any model is good.

**Acceptance:**
- [ ] Against a fake server with canned perfect answers, the scores are 100%. With canned hallucinations, the stripped counts match.
- [ ] `--compare` prints both labels.

**Verify:** `uv run pytest -q tests/test_eval.py`

**Files:** `eval.py`, `evals/` (tasks + fixtures), `tests/test_eval.py`, `pyproject.toml` · **Size:** M

**User step after the build:** run it once per candidate:
1. Set `model` in `~/.flint/server.toml`, then run `flint_server restart`.
2. Run `flint-eval --label flint-iq2xxs`.
3. Repeat for `gemma4-12b-q4`.
4. Run `flint-eval --compare flint-iq2xxs gemma4-12b-q4`.

Record the winner in ADR 0001's status.

### ⛳ Checkpoint D: done, so report to the user
- [ ] The bake-off has been run by the user and the winner recorded in ADR 0001.
- [ ] SPEC Success Criteria 1–5 are met. Number 1 and the live tests need the user's Flint running, so ask the user to run `uv run pytest -m live` and confirm the tools in the Code tab.
- [ ] Hand off the one-week trial (Success Criterion 6) to the user. Nothing more to build.

---

# Phase 5: Measured savings and leaner reports

Goal (2026-10-25): spend fewer Claude tokens per Flint call, measure the savings on real calls
instead of only the eval tasks, and make sure Claude stops Flint when it is done.

All token figures are `est_tokens` (chars/4). *Avoided* is what Claude would have spent doing
the job itself; *cost* is what the delegation cost Claude: the arguments it wrote (output) plus the
report and anything it must read to check it (input). Net savings = Σavoided over delegations that
delivered − Σcost over every Flint call, so failed delegations count against Flint.

## T14: Compact reports on the wire

**Do:** FastMCP sends a returned dict as `indent=2` JSON. Register the tools so the text Claude
receives is compact JSON (`separators=(",", ":")`, `ensure_ascii=False`) and has no duplicate
structured content. In `flint_read` reports, when every claim's file shares a folder, report it
once as `base` and give each claim's `file` relative to it. `report_tokens` measures that wire text.

**Acceptance:**
- [ ] Calling a tool through `mcp.call_tool` returns one text block of compact JSON.
- [ ] A read report with two claims in one folder carries `base` and relative `file`s.
- [ ] The tool functions still return dicts (existing tests unchanged).

**Files:** `server.py`, `tests/test_wire.py`, `README.md` · **Size:** S

## T15: Per-call cost and avoided tokens in the log

**Do:** every logged call gets `cost_in` (report + lines Claude reads to check it) and `cost_out`
(the arguments Claude wrote). Delivered work also gets `avoided_in` / `avoided_out`:
- `flint_read` ok: avoided_in = tokens of the file text Flint actually saw (focused reads count
  only the shown blocks, so a broad glob isn't credited as if Claude would read all of it).
  cost_in adds the cited lines (±2) Claude reads to verify.
- `flint_write` staged: cost_in adds the patch Claude must read. The editable text and the added
  lines are saved in the staging meta, and credited as avoided_in / avoided_out on `flint_apply`.
- `flint_ask` ok: avoided_out = the draft's tokens.

**Acceptance:**
- [ ] Unit tests for each tool's row, including a rejected write that has cost but no avoided.

**Files:** `server.py`, `savings.py`, `write.py`, `read.py`, `tests/test_cost_log.py` · **Size:** M

## T16: `flint-stats` savings and accuracy

**Do:** `summarize` adds `savings` {avoided_in, avoided_out, cost_in, cost_out, net_in, net_out,
net_weighted (output ×OUT_WEIGHT), saved_pct, per_tool} and `accuracy` {claims_kept,
claims_stripped, strip_rate, write accept_rate, unverifiable}. `render` prints both. Old rows
without the new fields are counted as calls but skipped in savings (reported as `unmeasured`).

**Acceptance:**
- [ ] Hand-computed totals on a fixture log; old rows are reported as unmeasured.

**Files:** `stats.py`, `tests/test_stats.py`, `README.md` · **Size:** S

## T17: Lifecycle guidance

**Do:** INSTRUCTIONS, the `flint_server` description and `CLAUDE.md` / the snippet say: Flint
autostarts on the next Flint call, so never call start first; when you have finished the
delegated work for the user's request, call `flint_server("stop")`. `flint_server` stop reports
the net tokens saved since the server started, so the stop is also the session's savings receipt.

**Acceptance:**
- [ ] Description tests require the new phrases; stop returns `saved_since_start`.

**Files:** `server.py`, `CLAUDE.md`, `docs/spec/claude-md-snippet.md`, `tests/test_descriptions.py`,
`tests/test_flint_server.py` · **Size:** S

---

# Phase 6: Read accuracy and any local model

Benchmark of 2026-10-25 (`phase5-compact`): read answer-correct fell to 65% (75% in
`focus-regions`, 80% in `flint-iq2xxs`). Probing the failing tasks showed two causes:
- Flint answers by reference ("the keys listed in PROTECTED", "the values in the DECIDED
  tuple"): the facts sit only in the claim quote, which the report drops, so Claude gets an answer
  it has to look up anyway.
- `temperature 0.2` makes runs disagree: `decided-outcomes` failed in the benchmark and passed
  in the probe right after, on the same file.

## T18: Concrete answers and deterministic sampling

**Do:** SYSTEM_READ and SYSTEM_MERGE require the answer to state names, values and numbers
themselves, never refer to them by identifier only. `[server] temperature` in flint.toml,
default 0. Eval results keep each read's `answer` so failures can be diagnosed.

**Acceptance:**
- [ ] The prompt test requires the wording; the request payload uses the configured temperature.
- [ ] Live: re-run the read tasks and record answer-correct.

## T19: Any OpenAI-compatible local server

**Do:** `[server] model` in flint.toml (default `flint`, the llama-server alias) is the model id
sent with every request, so Ollama, LM Studio, mlx_lm.server or vLLM can be used by setting
`base_url` and `model`. Health falls back from `/health` to `/v1/models` (served by all of them);
`ctx` comes from `/props` when present, else flint.toml. Without `~/.flint/server.toml` the bridge
just uses the running server and `flint_server` says it is not managed. Docs list each server's
settings.

**Acceptance:**
- [ ] Tests: model id in payload; health via `/v1/models` when `/health` is 404; status reports the model.
