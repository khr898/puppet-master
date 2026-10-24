# ADR 0001: Local model choice

- **Status:** Accepted (provisional default confirmed by the 2026-10-25 bake-off; Gemma 4 12B Q4 was not compared)
- **Date:** 2026-10-24

## Context

The bridge needs a local model that can:
- summarize code accurately and cite exact quotes,
- write whole files for boilerplate and mechanical edits,
- return strict JSON,
- run fast enough to beat the Claude round-trip.

The constraint is an M3 Pro with **18GB** of unified memory. The GPU gets about 12GB by default, and macOS plus Claude Desktop need about 4–5GB.

## What was checked (2026-10-24)

| Candidate | Size at a usable quant | Fits 18GB? | Notes |
|---|---|---|---|
| **Flint 40B-A2.5B MoE**, `unsloth/flint-GGUF` UD-IQ2_XXS | 11.86 GB | Yes (tight) | Only 2.5B parameters are active per token, so it's fast: Unsloth reports 125+ tok/s on an M4 Pro at 4-bit. Strong vendor-reported scores (74.2% SWE-bench Verified). At ~2.4 bits it loses a lot of fidelity, and community tests report only ~85–88% top-1 agreement *even at 4-bit*. **It's a reasoning model** (see Consequences). |
| Flint UD-Q2_K_XL / IQ2_M | 13.6 / 14.9 GB | No | Over the GPU limit, so macOS swaps. |
| Qwen 3.6 / 3.8 27B (dense) | Q4 ≈ 16.8 GB | No | Higher vendor-reported coding scores (77.2 Verified), but needs a 24GB Mac. At Q2 it would be both degraded and slow (27B active). |
| Devstral Small 2 (24B) | Q4 ≈ 15 GB | No | |
| Gemma 4 26B-A4B / Qwen 3.5–3.6 35B-A3B MoE | Q4 17.6–24 GB | No | |
| **Gemma 4 12B** (dense) | Q4 ≈ 7–8 GB | Yes, with room to spare | The most common 12GB-class coding pick: 72.0% LiveCodeBench v6, vendor-reported. It leaves room for more context. 12B active parameters make it slower than Flint per token. |
| Qwen 3.5 9B / Ornith 1.5 9B | Q4–Q6 ≈ 6–8 GB | Yes | Weaker, but would leave the most headroom. |

None of these numbers come from an independent test of *this* job (citing exact quotes, whole-file edits). Every benchmark above is vendor-reported or from a single tester, run on different harnesses.

## Decision

1. **Default: Flint UD-IQ2_XXS.** It's the only "big-brain" model that fits, and its MoE speed suits a delegation tool.
2. **Run a bake-off before the trial** (T13). Run Flint UD-IQ2_XXS against Gemma 4 12B at Q4 using the bridge's own `flint-eval` task set, and keep whichever scores better on:
   - the verified-claim rate,
   - the write check-pass rate,
   - speed (tok/s),
   - peak memory.

   The bridge doesn't depend on the model: switching means changing `model` in `~/.flint/server.toml`. The `--alias flint` name stays the same.
3. **One model at a time.** The "single local model" scope from the intent still holds; the bake-off only picks which one.

## Consequences

- **Reasoning tokens.** Flint emits `<think>` blocks, and its chat template has no `enable_thinking` switch. Thinking tokens share the 16K window and the `max_tokens` budget. The bridge handles this as follows (see SPEC → Reasoning control):
  - `--reasoning-budget 0` is the default.
  - Checkpoint A verifies it empirically.
  - If thinking can't be suppressed, a `reasoning_reserve` is taken out of the chunk budget.
- **Sampling.** Recursive recommends `temperature 1.0, top_p 0.95, top_k 20` for general use. The bridge uses `temperature 0.2`, because quote extraction and mechanical edits need determinism. The bake-off checks that this doesn't hurt quality.
- **Possible build branch.** Unsloth's Flint guide points at its `iq1-narrow` llama.cpp branch for its low-bit quants. If mainline llama.cpp can't load UD-IQ2_XXS, use that branch (see the setup doc).
- **Checkpoint A reasoning check (2026-10-24, measured).** With `--reasoning-budget 0`, a `json_schema` request returned valid JSON in `content`, with 0 chars of `reasoning_content` and no `<think>` text (20 completion tokens). `/props` reported `n_ctx` 16384. The `json_schema` grammar and the reasoning setting work together, so the `reasoning_reserve` fallback isn't needed for now. Answer quality with thinking off is still unmeasured (the T13 bake-off covers it). Free memory read 9% (about 1.7GB) with Flint loaded, so the memory preflight will rightly refuse a second start under this load.
- **Bake-off result (2026-10-25, measured, Flint UD-IQ2_XXS only).** `flint-eval --label flint-iq2xxs` at `temperature 0.2`, `--reasoning-budget 0`, `ctx` 16384, saved in `evals/results/flint-iq2xxs.json`:
  - read: 80% answer-correct, 1.4 verified claims kept and 0.1 stripped per task, 0 unverifiable;
  - write: 100% check-pass;
  - speed: 34.6 tok/s, 0 reasoning tokens;
  - peak RSS 1.88GB (server process only; the model is memory-mapped, so this understates the real footprint);
  - 0 tasks with errors.

  **Gemma 4 12B Q4 was not run**, by the user's choice, so this confirms Flint as the default but doesn't show it beats the alternative. Flint stays the default; the comparison remains open (`flint-eval --compare`) if the one-week trial shows a quality or memory problem.

## Sources

- [unsloth/flint-GGUF](https://huggingface.co/unsloth/flint-GGUF) and the [Unsloth Flint guide](https://unsloth.ai/docs/models/flint)
- [Atomic Agent: Recursive Flint test](https://atomicagent.io/blog/recursive-flint/)
- [RecursiveSI/flint model card](https://huggingface.co/RecursiveSI/flint)
- [aiweekly](https://aiweekly.co/alerts/recursive-opens-flint-a-40b25b-moe-coder-hitting-897-on-swe-bench-pro-v2-under) and [MarkTechPost](https://www.marktechpost.com/2026/10/21/recursive-flint-local-coding-model/)
- [LLMCheck: best local coding LLM for Mac (Oct 2026)](https://llmcheck.net/blog/best-local-llm-for-coding-mac/) and [PromptQuorum: best coding model for 12GB](https://www.promptquorum.com/prompt-bites/best-local-llm-coding-12gb-vram)
- [Atomic Chat: best local LLMs for 12GB](https://atomic.chat/blog/guides/best-local-llm-12gb)
