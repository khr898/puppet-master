# Measured results

All numbers come from saved runs in [`evals/results`](../evals/results). Token figures are estimates
(`chars / 4`, output tokens weighted 5x) of what the planner model (Claude) would have spent doing the
same work itself versus delegating it. They are not billed tokens.

## Test machine and models

- Apple Silicon Mac, M3 Pro, 18 GB unified memory, llama.cpp with Metal.
- Default worker: Flint `UD-IQ2_XXS` (40B-A2.5B MoE, 2-bit, 11.86 GB), 16K context, temperature 0,
  thinking off.
- Comparison worker: Mellum2.1 12B-A2.5B, Q4_K_M, 8.1 GB.

## Retrieval: BM25 focused reads and region edits

**Problem.** Claude only saves tokens on work it delegates. Two kinds of work stayed with it: finding
code (a read needed the right file up front, so Claude still grepped), and editing big files (the
whole file went to the worker and back, which at about 35 tok/s hit the timeout past ~3k tokens).

**Approach.** Both use one small model-free module. Split files into top-level blocks, score each
block against the question or task with BM25 over identifier terms, and double the score of a block
that *defines* a name the query mentions.

- *Focused read:* when the files need more than one chunk, the worker sees only the best blocks, up
  to `focus_tokens` (3k). Blocks under 20% of the best score are dropped; the top three bring along
  the same-file blocks that define the names they use. Lines not shown cannot be cited.
  `exhaustive=true` reads everything instead.
- *Region edit:* for files over `region_tokens` (2k), the worker sees an outline plus the header and
  at most four matching blocks, and returns only those regions. They are spliced back, so the patch,
  the check and the review are unchanged.
- Prompt compression was rejected: it is lossy, which breaks exact-quote verification.
- Dense embeddings were not used: BM25 already ranked the answer block first, and an embedding model
  would compete with the worker for memory.

**Results** (Flint, 27 tasks, 0 errors):

| Group | Without delegation | With | Saved | Quality |
|---|---|---|---|---|
| Big-file writes (previously too slow or over budget) | 10,806 | 392 | **96%** | 2/2 pass, ~4 s each |
| Reads over a whole package (31.8k tokens) | 4,095 | 3,132 | 24% | 8/10 correct, one call each, ~5 s |
| Original 15 tasks (code paths unchanged) | 5,107 | 3,286 | 36% | reads 7/10, writes 5/5 |
| **All 27** | 20,008 | 6,810 | **66%** | |

- The package-read baseline counts only reading the file that holds the answer; Claude's grep and
  wrong guesses are not counted, so the real saving is larger.
- Searching 31.8k tokens was as accurate as handing the worker the exact file (8/10 vs 7/10).
- Offline, the answer block ranked first for 10/10 questions.
- The original 15 tasks fell from 48% to 36% saved with no code change on their path; two reads that
  passed before failed this time. That is run-to-run variance on a small set.

**Risks.** A focused read can miss an answer spread over many weakly matching blocks (use
`exhaustive=true` for whole-file questions). A region edit cannot touch code outside its regions; the
check catches resulting breakage. Name the function in a write task, or no block matches.

## Worker model comparison

Same code and 27 tasks, final configuration:

| | Flint IQ2_XXS | Mellum2.1, thinking off | Mellum2.1, thinking on |
|---|---|---|---|
| Read answers correct | **90%** | 60% | 85% |
| Write checks pass | **86%** | 71% | 71% |
| Claude tokens saved | **74%** | 58% | 57% |
| Avg model time per read | 7.8 s | 7.0 s | 89 s |
| Model size | 11.9 GB | 8.1 GB | 8.1 GB |

A re-run of the default model gave 85% reads, 86% writes, 74.2% saved, 26.4 tok/s: treat reads as
roughly 85-90%. A small model with thinking off tends to answer by reference ("the listed keys")
instead of stating values, which a prompt change (concrete values) fixed for the default model.
Accuracy depends strongly on the model and its settings; no larger or higher-precision model has been
benchmarked yet.

## Test suite

355 passed, 22 skipped (tests that need a free network port), about 89% line coverage; the guardrail
modules (read, write, check, report) are at 96% or higher. Ruff lint and format are clean.
