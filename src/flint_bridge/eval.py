import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from functools import partial
from pathlib import Path

from flint_bridge import lifecycle, savings, server
from flint_bridge.config import Config, load_config
from flint_bridge.llm import FlintOffline
from flint_bridge.read import Usage, flint_read_impl
from flint_bridge.write import flint_write_impl

Ready = Callable[[], Awaitable[dict | None]] | None


def answer_correct(answer: str, must_mention: list[str]) -> bool:
    """True if the answer contains every keyword, ignoring case."""
    # Whole-word match, so a keyword like "2" doesn't match the "2" inside "12".
    return all(re.search(rf"(?<!\w){re.escape(k)}(?!\w)", answer, re.I) for k in must_mention)


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _pct(hits: int, n: int) -> float | None:
    return 100 * hits / n if n else None


# --- tasks ---------------------------------------------------------------------------------


async def run_read_task(cfg: Config, task: dict, repo: Path, ready: Ready = None) -> dict:
    paths = [f"{repo}/{p}" for p in task["paths"]]
    usage = Usage()
    try:
        out = await flint_read_impl(
            cfg, paths, task["question"], usage=usage, ready=ready, focus=True
        )
    except FlintOffline:
        out = {"error": "flint_offline"}
    correct = "answer" in out and answer_correct(out["answer"], task["must_mention"])
    return {
        "id": task["id"],
        "error": out.get("error"),
        "correct": correct,
        "answer": out.get("answer"),
        # What Claude would have read without Flint: the files that hold the answer.
        "savings": savings.read_savings(
            [f"{repo}/{p}" for p in task.get("baseline_paths", task["paths"])], out, ok=correct
        ),
        "focus": out.get("focus"),
        "claims": len(out.get("claims", [])),
        "stripped": out.get("stripped", 0),
        "unverifiable": out.get("error") == "unverifiable_output",
        "tokens_out": usage.tokens_out,
        "reasoning_tokens": usage.reasoning_tokens,
        "model_seconds": usage.model_seconds,
    }


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


async def run_write_task(cfg: Config, task: dict, evals_dir: Path, ready: Ready = None) -> dict:
    """Copy the task's fixture into a throwaway git repo and have Flint fix it there."""
    checks = {"test": [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"]}
    cfg = replace(cfg, check_default="test", check_commands=checks)
    usage = Usage()
    with tempfile.TemporaryDirectory(prefix="flint-eval-") as tmp:
        repo = Path(tmp).resolve() / "proj"
        shutil.copytree(
            evals_dir / "fixtures" / task["fixture"],
            repo,
            ignore=shutil.ignore_patterns("solution", "__pycache__"),
        )
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "eval@example.com")
        _git(repo, "config", "user.name", "eval")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fixture")
        files = [str(repo / f) for f in task["files"]]
        texts = [Path(f).read_text() for f in files]
        try:
            out = await flint_write_impl(cfg, task["task"], files, "", usage=usage, ready=ready)
        except FlintOffline:
            out = {"error": "flint_offline"}
        patch = Path(out["patch_path"]).read_text() if "patch_path" in out else None
    passed = bool(out.get("check", {}).get("passed"))
    return {
        "id": task["id"],
        "error": out.get("error"),
        "passed": passed,
        "savings": savings.write_savings(texts, patch, out, passed=passed),
        "tokens_out": usage.tokens_out,
        "reasoning_tokens": usage.reasoning_tokens,
        "model_seconds": usage.model_seconds,
    }


# --- memory --------------------------------------------------------------------------------


class RssPeak:
    """Highest llama-server memory reading seen, polled while the tasks run."""

    def __init__(self, pid_fn: Callable[[], int | None], interval: float = 1.0):
        self.pid_fn = pid_fn
        self.interval = interval
        self.peak: float | None = None

    def sample(self) -> None:
        pid = self.pid_fn()
        rss = lifecycle.rss_gb(pid) if pid else None
        if rss is not None and (self.peak is None or rss > self.peak):
            self.peak = rss

    async def run(self) -> None:
        while True:
            await asyncio.to_thread(self.sample)
            await asyncio.sleep(self.interval)


# --- scoring -------------------------------------------------------------------------------


def summarize(reads: list[dict], writes: list[dict], peak_gb: float | None) -> dict:
    everything = reads + writes
    seconds = sum(r["model_seconds"] for r in everything)
    return {
        "read": {
            "n": len(reads),
            "correct_pct": _pct(sum(r["correct"] for r in reads), len(reads)),
            "avg_claims_kept": _avg([r["claims"] for r in reads]),
            "avg_stripped": _avg([r["stripped"] for r in reads]),
            "unverifiable": sum(r["unverifiable"] for r in reads),
        },
        "write": {
            "n": len(writes),
            "check_pass_pct": _pct(sum(w["passed"] for w in writes), len(writes)),
        },
        "speed": {
            "tok_per_s": sum(r["tokens_out"] for r in everything) / seconds if seconds else None,
            "avg_reasoning_tokens": _avg([r["reasoning_tokens"] for r in everything]),
        },
        "memory": {"peak_rss_gb": peak_gb},
        "savings": {
            "read": savings.totals([r["savings"] for r in reads]),
            "write": savings.totals([w["savings"] for w in writes]),
            "all": savings.totals([r["savings"] for r in everything]),
        },
        # Tasks that failed for a reason other than the model's answer (server down, over budget).
        "run": {"errors": sum(r["error"] not in (None, "unverifiable_output") for r in everything)},
    }


async def run_eval(
    cfg: Config,
    evals_dir: Path,
    *,
    pid_fn: Callable[[], int | None],
    ready: Ready = None,
) -> dict:
    """Run every task through the real read and write code against the running server."""
    repo = evals_dir.resolve().parent
    read_tasks = json.loads((evals_dir / "read_tasks.json").read_text())
    write_tasks = json.loads((evals_dir / "write_tasks.json").read_text())
    rss = RssPeak(pid_fn)
    poller = asyncio.create_task(rss.run())
    try:
        reads = [await run_read_task(cfg, t, repo, ready) for t in read_tasks]
        writes = [await run_write_task(cfg, t, evals_dir.resolve(), ready) for t in write_tasks]
    finally:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)
    return {"summary": summarize(reads, writes, rss.peak), "reads": reads, "writes": writes}


# --- output --------------------------------------------------------------------------------

ROWS = [
    ("read: answer-correct", "read", "correct_pct", "{:.0f}%"),
    ("read: avg claims kept", "read", "avg_claims_kept", "{:.1f}"),
    ("read: avg stripped", "read", "avg_stripped", "{:.1f}"),
    ("read: unverifiable", "read", "unverifiable", "{:d}"),
    ("write: check-pass", "write", "check_pass_pct", "{:.0f}%"),
    ("speed: tok/s", "speed", "tok_per_s", "{:.1f}"),
    ("speed: avg reasoning tokens", "speed", "avg_reasoning_tokens", "{:.0f}"),
    ("memory: peak RSS (GB)", "memory", "peak_rss_gb", "{:.2f}"),
    ("run: tasks with errors", "run", "errors", "{:d}"),
]


def render_table(results: dict[str, dict]) -> str:
    """One column per label, one row per metric."""
    width = 30
    lines = ["".ljust(width) + "".join(label.rjust(18) for label in results)]
    for name, group, key, fmt in ROWS:
        cells = []
        for res in results.values():
            value = res["summary"][group][key]
            cells.append("n/a" if value is None else fmt.format(value))
        lines.append(name.ljust(width) + "".join(c.rjust(18) for c in cells))
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flint-eval", description="Score the running model on the bridge's own tasks."
    )
    parser.add_argument("--label", help="name for this run, e.g. flint-iq2xxs")
    parser.add_argument("--compare", nargs=2, metavar=("A", "B"), help="print two saved runs")
    parser.add_argument("--evals-dir", type=Path, default=Path("evals"))
    parser.add_argument("--results-dir", type=Path, help="default: <evals-dir>/results")
    return parser


async def amain(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.label and not args.compare:
        parser.error("give --label NAME to run, or --compare A B")
    evals_dir = args.evals_dir
    results_dir = args.results_dir or evals_dir / "results"

    if args.compare:
        loaded = {}
        for label in args.compare:
            path = results_dir / f"{label}.json"
            if not path.exists():
                print(f"No saved run called {label!r} at {path}")
                return 1
            loaded[label] = json.loads(path.read_text())
        print(render_table(loaded))
        return 0

    code, _ = await run_and_save(evals_dir, results_dir, args.label)
    return code


async def run_and_save(evals_dir: Path, results_dir: Path, label: str) -> tuple[int, dict]:
    """Run every task against the live server, save the result, print the score table."""
    cfg = load_config(evals_dir.resolve().parent)
    try:
        sc = lifecycle.load_server_config()
    except ValueError:
        sc = None
    started = time.perf_counter()
    result = await run_eval(
        cfg,
        evals_dir,
        pid_fn=lambda: lifecycle.managed_pid(sc) if sc else None,
        ready=partial(server.ensure_up, cfg),
    )
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / f"{label}.json").write_text(json.dumps(result, indent=2))
    print(render_table({label: result}))
    if errors := result["summary"]["run"]["errors"]:
        print(
            f"\nWARNING: {errors} task(s) errored (server down or over budget), not wrong answers."
        )
    print(f"\n{round(time.perf_counter() - started)}s; saved to {results_dir / label}.json\n")
    return 0, result


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(amain(argv))
