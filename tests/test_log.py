import json
from pathlib import Path

import pytest

from flint_bridge.log import log_call

FIELDS = {
    "ts", "tool", "task_key", "duration_ms", "report_tokens",
    "flint_tokens_in", "flint_tokens_out", "outcome", "stripped", "retries",
}  # fmt: skip


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_writes_one_json_line_with_all_fields(tmp_path: Path):
    log_call(tmp_path, tool="flint_read", outcome="ok", duration_ms=12)
    [row] = read_lines(tmp_path / ".flint" / "log.jsonl")
    assert FIELDS <= set(row)
    assert row["tool"] == "flint_read"
    assert row["duration_ms"] == 12
    assert row["task_key"] is None
    assert row["ts"].endswith("+00:00")


def test_appends_instead_of_overwriting(tmp_path: Path):
    log_call(tmp_path, tool="a", outcome="ok")
    log_call(tmp_path, tool="b", outcome="error")
    assert [r["tool"] for r in read_lines(tmp_path / ".flint" / "log.jsonl")] == ["a", "b"]


def test_none_root_logs_to_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    log_call(None, tool="flint_status", outcome="ok")
    assert (tmp_path / ".flint" / "log.jsonl").exists()


def test_unknown_outcome_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError):
        log_call(tmp_path, tool="x", outcome="great")
    assert not (tmp_path / ".flint" / "log.jsonl").exists()


@pytest.mark.parametrize(
    "outcome",
    [
        "ok",
        "error",
        "check_failed",
        "staged",
        "applied",
        "rejected",
        "miss",
        "offline",
        "over_budget",
    ],
)
def test_all_documented_outcomes_are_accepted(tmp_path: Path, outcome: str):
    log_call(tmp_path, tool="x", outcome=outcome)
