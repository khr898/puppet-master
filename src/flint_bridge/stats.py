import argparse
import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

DECIDED = ("applied", "rejected", "miss", "check_failed")
_SINCE = re.compile(r"^(\d+)([dh])$")


def parse_since(since: str) -> timedelta:
    m = _SINCE.match(since)
    if m is None:
        raise ValueError(f"--since must look like 7d or 24h, got {since!r}")
    n = int(m.group(1))
    return timedelta(days=n) if m.group(2) == "d" else timedelta(hours=n)


def load_rows(path: Path, since: str | None = None) -> list[dict]:
    """Read the call log, optionally keeping only rows newer than `since` (7d, 24h)."""
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if since is None:
        return rows
    cutoff = datetime.now(UTC) - parse_since(since)
    return [r for r in rows if datetime.fromisoformat(r["ts"]) >= cutoff]


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize(rows: list[dict]) -> dict:
    outcomes = Counter(r["outcome"] for r in rows if r.get("outcome"))
    decided = sum(outcomes[o] for o in DECIDED)

    def values(key: str, tool: str | None = None) -> list[float]:
        return [
            r[key] for r in rows if r.get(key) is not None and (tool is None or r["tool"] == tool)
        ]

    tools = sorted({r["tool"] for r in rows})
    return {
        "calls": dict(Counter(r["tool"] for r in rows)),
        "outcomes": dict(outcomes),
        "accept_rate": outcomes["applied"] / decided if decided else None,
        "avg_report_tokens": _avg(values("report_tokens")),
        "tokens_in": int(sum(values("flint_tokens_in"))),
        "tokens_out": int(sum(values("flint_tokens_out"))),
        "avg_duration_ms": {t: _avg(values("duration_ms", t)) for t in tools},
    }


def _fmt(value: float | None, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value:.1f}{suffix}"


def render(summary: dict) -> str:
    lines = ["Calls per tool:"]
    lines += [f"  {tool}: {n}" for tool, n in sorted(summary["calls"].items())]
    lines.append("Outcomes:")
    lines += [f"  {outcome}: {n}" for outcome, n in sorted(summary["outcomes"].items())]
    rate = summary["accept_rate"]
    lines.append(f"Accept rate: {_fmt(None if rate is None else rate * 100, '%')}")
    lines.append(f"Average report tokens: {_fmt(summary['avg_report_tokens'])}")
    lines.append(f"Flint tokens: {summary['tokens_in']} in, {summary['tokens_out']} out")
    lines.append("Average duration per tool (ms):")
    lines += [f"  {tool}: {_fmt(ms)}" for tool, ms in summary["avg_duration_ms"].items()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="flint-stats", description="Summarize the Flint call log."
    )
    parser.add_argument("--log", type=Path, default=Path(".flint/log.jsonl"))
    parser.add_argument("--since", help="only calls newer than this, e.g. 7d or 24h")
    args = parser.parse_args(argv)
    if not args.log.exists():
        print(f"No log at {args.log}")
        return 1
    try:
        rows = load_rows(args.log, args.since)
    except ValueError as e:
        parser.error(str(e))
    print(render(summarize(rows)))
    return 0
