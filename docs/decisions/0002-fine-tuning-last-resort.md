# ADR 0002: Fine-tuning is a last resort

- **Status:** Deferred (logged; revisit only when the triggers below fire)
- **Date:** 2026-10-24

## Context

The question was whether to fine-tune Flint, or a model like it, into a specialist for this job: verified-citation reads, whole-file edits, strict JSON.

## Findings (2026-10-24)

- **Flint can't be fine-tuned on this Mac.**
  - Unsloth has no Flint fine-tuning notebook.
  - Unsloth's generic minimums are 40B QLoRA at 30GB and 40B LoRA at 96GB, and those are "absolute minimum" figures. A third-party calculator estimates LoRA at 65GB (INT8) to 130GB (FP16).
  - All of these are above 18GB, so the training would need a rented 48–80GB GPU.
- **The quantization afterwards undoes part of the gain.** The tuned model would have to be re-quantized to ~2.4 bits (UD-IQ2_XXS) to fit, and that can wipe out small fine-tuning gains.
- **A smaller model could be fine-tuned locally.** Unsloth supports training on Mac, and MLX LoRA on a 4-bit ≤9B model fits in 18GB (12B is tight). A small, task-specialized model is the realistic local fine-tuning target.
- **Most of the job's failure modes are already handled without training:**
  - JSON-schema grammar makes invalid structure impossible.
  - Mechanical quote verification removes invented citations.
  - Staged patches, a check run and Claude's review stop bad writes.
- **There's no training data yet.** That's why the bridge now captures samples (SPEC → Sample capture).

## Decision

**No fine-tuning in v1.** It is the **last** lever, after these, in order:

1. Prompt tuning: tighter system prompts, and one short example per tool (measure the context it costs).
2. Sampling: temperature 0.1–0.3, and reasoning control.
3. The model bake-off (ADR 0001 / T13), plus a bigger quant if memory allows.
4. Narrower delegation: stop sending task types that the log shows failing.

## Triggers to reopen

Reopen this decision only if **all** of these are true after the one-week trial and steps 1–4:

- The accept rate is below **70%** within 2 attempts, or more than **25%** of claims are stripped per `puppet_read`.
- The failures cluster in a learnable pattern (for example, the same kind of wrong edit). Random misses don't count.
- `.puppet-master/samples/` has at least **~500 labeled samples** (accepted vs rejected/stripped).

## If reopened, the preferred order

1. **Local MLX/Unsloth LoRA on a small dense model** (≤9B at 4-bit, or Gemma 4 12B if it fits), trained on the accepted samples. It's cheap, private and repeatable.
2. **Rented-GPU QLoRA of Flint** (≥30GB, realistically a 48–80GB GPU), followed by GGUF conversion and re-quantization to UD-IQ2_XXS. Re-run the T13 bake-off to prove it beats the base model *after* quantization.

## Log

| Date | Event |
|---|---|
| 2026-10-24 | Deferred. No data yet, and Flint can't be trained locally. Sample capture was added so the option stays open. |

## Sources

- [Unsloth requirements](https://unsloth.ai/docs/get-started/fine-tuning-for-beginners/unsloth-requirements) and the [Unsloth Flint guide](https://unsloth.ai/docs/models/flint)
- [Spheron: Flint VRAM calculator](https://www.spheron.network/tools/gpu-recommender/RecursiveSI/flint/)
