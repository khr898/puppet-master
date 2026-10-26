# Intent: Usage Saver (Flint delegation)

Confirmed 2026-10-24.

- **Outcome:** In Claude Desktop's Code tab, Claude dynamically decides what to hand to local Flint and what to do itself; Claude keeps planning, hard problems, and final review.
- **User:** Solo developer on Claude Pro, M3 Pro with 18GB unified memory.
- **Why now:** Long exploring + writing sessions hit the 5-hour limit.
- **Success:** Fewer limit hits over a one-week before/after comparison, with zero Flint mistakes landing in the project.
- **Constraint (mandatory checks, never skipped):**
  - Reads: every claim in a Flint report cites `file:line` + an exact quote; the bridge verifies each mechanically and strips failures. Claude re-reads the cited lines before acting on a claim.
  - Writes: Flint writes only to a staging patch; `puppet_check` (tests + lint) must pass; Claude reads every diff before it is applied. Nothing reaches the repo without Claude's approval.
  - Failures: two failed checks, or Flint down/slow (>90s) → Claude takes over and logs it.
- **Wiring:** Flint registered via Claude Desktop's MCP config (app settings / `.json`), verified to show up in Code tab sessions. Delegation policy lives in `CLAUDE.md`, the MCP server instructions, and the tool descriptions.
- **Out of scope:** Fixed routing table or auto-classifier; optional or "light" checks; Flint writing directly to the repo; zero Claude usage; multiple local models; unrestricted shell.
