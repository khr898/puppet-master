import json
from datetime import UTC, datetime
from pathlib import Path

OUTCOMES = frozenset(
    {
        "ok",
        "error",
        "check_failed",
        "staged",
        "applied",
        "rejected",
        "miss",
        "offline",
        "over_budget",
    }
)
FIELDS = (
    "tool", "task_key", "duration_ms", "report_tokens",
    "flint_tokens_in", "flint_tokens_out", "outcome", "stripped", "retries",
)  # fmt: skip


def log_call(root: Path | None, **fields) -> None:
    """Append one JSON line to root/.flint/log.jsonl (or ~/.flint/log.jsonl when root is None)."""
    outcome = fields.get("outcome")
    if outcome is not None and outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    row = {"ts": datetime.now(UTC).isoformat()}
    row.update({k: fields.get(k) for k in FIELDS})
    row.update({k: v for k, v in fields.items() if k not in FIELDS})
    path = (root if root is not None else Path.home()) / ".flint" / "log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")
