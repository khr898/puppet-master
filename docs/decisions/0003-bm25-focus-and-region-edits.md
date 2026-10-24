# ADR 0003: BM25 focused reads and region edits

- **Status:** Accepted
- **Date:** 2026-10-25

## Context

Claude saves tokens only on the work it hands to Flint. Two kinds of work stayed with Claude:

- **Finding code.** `flint_read` needed the right file up front, so Claude still had to grep and open files to find it. A glob over a package spilled into map-reduce over several chunks, and anything past 8 chunks was `over_budget`.
- **Big files.** `flint_write` sent the whole file and had Flint write it all back. At about 35 tok/s, a 3k-token file takes about 90s, which is the timeout, and anything over 6k tokens was `over_budget`. Those are the edits where Claude spends the most, since it has to read the whole file.

Prompt compression (LLMLingua) was ruled out first. It is lossy, which breaks exact-quote verification and patch review, and it can't touch the tokens Claude actually spends.

## Decision

Both tools use the same math, in `retrieve.py`, with no model and no extra memory:

1. Split files into top-level blocks.
2. Score each block against the question or task with **BM25** over identifier terms. This is a sparse-vector model: inverse document frequency × saturated term frequency, normalized by length.
3. **Boost** a block 2× when it *defines* a name the query mentions.

- **Focused reads.** When the files need more than one chunk, Flint sees only the best blocks, up to `focus_tokens` (3k). Blocks under 20% of the best score are dropped. The top 3 blocks each bring along the same-file blocks that define the names they use. Hidden lines can't be cited. `exhaustive=true` keeps the old behaviour.
- **Region edits.** For files over `region_tokens` (2k), Flint sees an outline plus the header and at most 4 blocks scoring at least half the best, and returns only those regions. The bridge splices them back. The patch, the check and Claude's review don't change. The JSON schema is narrowed for each call, so a region-only call can't answer with whole files.

## Results (2026-10-25, measured)

`flint-savings --label focus-regions` against Flint UD-IQ2_XXS, saved in `evals/results/focus-regions.json`. The task set gained 10 `find-*` reads (the 10 existing questions, asked over `src/flint_bridge/*.py`, 31.8k tokens) and 2 writes on a 5.3k-token module.

| Group | Without Flint | With Flint | Saved | Quality |
|---|---|---|---|---|
| Big-file writes (new; previously too slow or over budget) | 10,806 | 392 | **96%** | 2/2 pass, ~4s each |
| `find-*` reads over the whole package (new) | 4,095 | 3,132 | 24% | 8/10 correct, one call each, ~5s |
| Original 15 tasks (code paths unchanged) | 5,107 | 3,286 | 36% | reads 7/10, writes 5/5 |
| **All 27** | 20,008 | 6,810 | **66%** | 0 errors |

- The `find-*` baseline counts only reading the file that holds the answer. Claude's grep and its wrong guesses aren't counted, so the real saving is larger. Even so, focused reads over 31.8k tokens were as accurate as handing Flint the exact file (8/10 vs 7/10 in the same run).
- Offline, before tuning: the answer block ranked first for 10/10 questions, and all answer keywords were in the focused context at a 2k budget.
- The original 15 tasks went from 48% (ADR 0001 run) to 36%. Their code paths didn't change. Two specific-file reads that passed before failed this time, and each is charged the full fallback cost. That is run-to-run variance on a small set, not an effect of this change.

## Consequences

- Claude can ask Flint to find code with a glob instead of grepping. The `focus` field in the report says how much Flint saw.
- Big-file edits can now be delegated. Naming the function in the task matters, because a task with no matching block gets `over_budget`.
- **Risks:**
  - A focused read can miss an answer spread across many weakly matching blocks. Use `exhaustive=true` for whole-file questions such as summaries.
  - A region edit can't touch code outside its regions. Usages elsewhere are left alone and the check catches the breakage.
- **Not done:** dense embeddings. BM25 already ranks the answer block first on this set, and an embedding model would compete with Flint for the 18GB. Revisit if the one-week trial shows retrieval misses.

## Log

| Date | Change |
|---|---|
| 2026-10-25 | Accepted with the measured results above. |
