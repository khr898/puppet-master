import argparse
import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from flint_bridge.savings import OUT_WEIGHT

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
    cutoff = None if since is None else datetime.now(UTC) - parse_since(since)
    rows = []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
            when = datetime.fromisoformat(row["ts"])
            row["tool"]
        except (ValueError, KeyError, TypeError):
            continue  # a truncated or hand-edited line must not hide the rest of the log
        if cutoff is None or when >= cutoff:
            rows.append(row)
    return rows


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _sum(rows: list[dict], key: str) -> int:
    return int(sum(r.get(key) or 0 for r in rows))


def savings(rows: list[dict]) -> dict:
    """Claude tokens avoided by delivered work, minus what every measured call cost Claude.

    Output tokens are weighted OUT_WEIGHT in the totals. Rows logged before costs were measured
    are left out and counted as `unmeasured`.
    """
    measured = [r for r in rows if r.get("cost_in") is not None]
    t = {k: _sum(measured, k) for k in ("avoided_in", "avoided_out", "cost_in", "cost_out")}
    net_in, net_out = t["avoided_in"] - t["cost_in"], t["avoided_out"] - t["cost_out"]
    avoided = t["avoided_in"] + OUT_WEIGHT * t["avoided_out"]
    per_tool: dict[str, dict] = {}
    for r in measured:
        tool = per_tool.setdefault(r["tool"], {"cost": 0, "avoided": 0})
        tool["cost"] += (r.get("cost_in") or 0) + OUT_WEIGHT * (r.get("cost_out") or 0)
        tool["avoided"] += (r.get("avoided_in") or 0) + OUT_WEIGHT * (r.get("avoided_out") or 0)
    net = net_in + OUT_WEIGHT * net_out
    return {
        "measured": len(measured),
        "unmeasured": len(rows) - len(measured),
        **t,
        "net_in": net_in,
        "net_out": net_out,
        "net_weighted": net,
        "saved_pct": 100 * net / avoided if avoided else None,
        "per_tool": per_tool,
    }


def accuracy(rows: list[dict]) -> dict:
    reads = [r for r in rows if r["tool"] == "flint_read" and r.get("claims") is not None]
    kept, stripped = _sum(reads, "claims"), _sum(reads, "stripped")
    return {
        "claims_kept": kept,
        "claims_stripped": stripped,
        "strip_rate": stripped / (kept + stripped) if kept + stripped else None,
        "unverifiable": sum(r.get("error") == "unverifiable_output" for r in rows),
    }


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
        "avg_reasoning_tokens": {
            t: avg for t in tools if (avg := _avg(values("reasoning_tokens", t))) is not None
        },
        "savings": savings(rows),
        "accuracy": accuracy(rows),
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
    if summary["avg_reasoning_tokens"]:
        lines.append("Average reasoning tokens per tool:")
        lines += [f"  {tool}: {_fmt(n)}" for tool, n in summary["avg_reasoning_tokens"].items()]
    return "\n".join(lines + _render_savings(summary["savings"]) + _render_accuracy(summary))


def _render_savings(s: dict) -> list[str]:
    pct = "" if s["saved_pct"] is None else f" ({s['saved_pct']:.0f}% of the work delegated)"
    lines = [
        f"Claude tokens saved (est., output x{OUT_WEIGHT}): {s['net_weighted']}{pct}",
        f"  input:  {s['avoided_in']} avoided - {s['cost_in']} spent = {s['net_in']}",
        f"  output: {s['avoided_out']} avoided - {s['cost_out']} spent = {s['net_out']}",
    ]
    for tool, t in sorted(s["per_tool"].items()):
        lines.append(f"  {tool}: {t['avoided']} avoided, {t['cost']} spent")
    if s["unmeasured"]:
        n = s["unmeasured"]
        lines.append(f"  ({n} call{'' if n == 1 else 's'} logged before costs were measured)")
    return lines


def _render_accuracy(summary: dict) -> list[str]:
    a = summary["accuracy"]
    total = a["claims_kept"] + a["claims_stripped"]
    rate = _fmt(None if a["strip_rate"] is None else a["strip_rate"] * 100, "%")
    return [
        "Accuracy:",
        f"  Claims stripped: {a['claims_stripped']} of {total} ({rate})",
        f"  Unverifiable outputs: {a['unverifiable']}",
    ]


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
