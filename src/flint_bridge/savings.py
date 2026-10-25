"""Estimate the Claude tokens a delegation saves, from the eval tasks.

Every figure is an estimate (`est_tokens`) of what Claude itself would spend, with and without
Flint, to get the same verified result. A delegation that doesn't pan out is charged the fallback:
Claude does the job after all, on top of what Flint already cost it.
"""

import argparse
import asyncio
import json
import re
from pathlib import Path

from flint_bridge.report import est_tokens

# An output token costs more than an input token. 5x is a round figure for the price ratio.
OUT_WEIGHT = 5
# Lines of context Claude reads around each claim it checks before acting on it.
VERIFY_RADIUS = 2

_RANGE = re.compile(r"^(.*):(\d+)-(\d+)$")


def _text(spec: str | Path) -> str:
    """File text, or just the lines of a `path:start-end` range."""
    m = _RANGE.match(str(spec))
    if not m:
        return Path(spec).read_text(errors="replace")
    lines = Path(m.group(1)).read_text(errors="replace").splitlines(keepends=True)
    return "".join(lines[int(m.group(2)) - 1 : int(m.group(3))])


def verify_tokens(result: dict) -> int:
    """Tokens Claude spends reading the lines each reported claim cites."""
    total = 0
    for c in result.get("claims", []):
        try:
            lines = Path(c["file"]).read_text(errors="replace").splitlines()
        except (OSError, KeyError):
            continue
        at = c.get("line", 1) - 1
        window = lines[max(0, at - VERIFY_RADIUS) : at + VERIFY_RADIUS + 1]
        total += est_tokens("\n".join(window))
    return total


def _row(baseline: int, with_flint: int, **extra) -> dict:
    return {"baseline": baseline, "with_flint": with_flint, "saved": baseline - with_flint, **extra}


def read_savings(specs: list, result: dict, *, ok: bool) -> dict:
    """Without Flint Claude reads the files; with it, the report and the lines it cites."""
    baseline = sum(est_tokens(_text(s)) for s in specs)
    report = est_tokens(json.dumps(result))
    verify = verify_tokens(result)
    return _row(baseline, report + verify + (0 if ok else baseline), verify=verify)


def added_tokens(patch: str | None) -> int:
    """Tokens of the lines a patch adds: what Claude would have written to make the change."""
    added = [ln[1:] for ln in (patch or "").splitlines() if ln.startswith("+") and ln[:3] != "+++"]
    return est_tokens("\n".join(added))


def write_savings(texts: list[str], patch: str | None, result: dict, *, passed: bool) -> dict:
    """Without Flint Claude reads the files and writes the change; with it, reads the patch."""
    out = added_tokens(patch)
    baseline = sum(est_tokens(t) for t in texts) + OUT_WEIGHT * out
    with_flint = est_tokens(patch or "") + est_tokens(json.dumps(result))
    return _row(baseline, with_flint + (0 if passed else baseline), baseline_out=out)


def totals(rows: list[dict]) -> dict:
    baseline = sum(r["baseline"] for r in rows)
    with_flint = sum(r["with_flint"] for r in rows)
    saved = baseline - with_flint
    return {
        "baseline": baseline,
        "with_flint": with_flint,
        "saved": saved,
        "saved_pct": 100 * saved / baseline if baseline else None,
    }


# --- output --------------------------------------------------------------------------------


def render_saved(result: dict) -> str:
    if "savings" not in result.get("summary", {}):
        return "no savings data in this run; re-run with flint-savings"
    lines = [f"{'task':26}{'without':>10}{'with':>10}{'saved':>10}"]
    for r in result["reads"] + result["writes"]:
        s = r["savings"]
        lines.append(f"{r['id']:26}{s['baseline']:>10}{s['with_flint']:>10}{s['saved']:>10}")
    for name in ("read", "write", "all"):
        t = result["summary"]["savings"][name]
        pct = "n/a" if t["saved_pct"] is None else f"{t['saved_pct']:.0f}%"
        label = "TOTAL" if name == "all" else f"  {name}s"
        lines.append(f"{label:26}{t['baseline']:>10}{t['with_flint']:>10}{t['saved']:>10}  {pct}")
    lines.append(f"(estimated Claude tokens; output tokens weighted {OUT_WEIGHT}x)")
    return "\n".join(lines)


async def amain(argv: list[str] | None = None) -> int:
    from flint_bridge import eval as flint_eval  # eval imports this module

    parser = argparse.ArgumentParser(
        prog="flint-savings",
        description="Estimate the Claude tokens Flint saves on the eval tasks.",
    )
    parser.add_argument("--label", default="savings", help="name for the saved run")
    parser.add_argument("--show", metavar="LABEL", help="print a saved run instead of running")
    parser.add_argument("--evals-dir", type=Path, default=Path("evals"))
    parser.add_argument("--results-dir", type=Path, help="default: <evals-dir>/results")
    args = parser.parse_args(argv)
    results_dir = args.results_dir or args.evals_dir / "results"

    if args.show:
        path = results_dir / f"{args.show}.json"
        if not path.exists():
            print(f"No saved run called {args.show!r} at {path}")
            return 1
        print(render_saved(json.loads(path.read_text())))
        return 0

    code, result = await flint_eval.run_and_save(args.evals_dir, results_dir, args.label)
    print(render_saved(result))
    return code


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(amain(argv))
