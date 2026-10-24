import json
from datetime import UTC, datetime, timedelta

import pytest

from flint_bridge import stats


def row(tool, outcome, *, days_ago=0.0, **fields) -> dict:
    ts = datetime.now(UTC) - timedelta(days=days_ago)
    return {"ts": ts.isoformat(), "tool": tool, "outcome": outcome, **fields}


@pytest.fixture
def log_path(tmp_path):
    rows = [
        row("flint_read", "ok", report_tokens=100, flint_tokens_in=1000, flint_tokens_out=50,
            duration_ms=2000),
        row("flint_read", "ok", report_tokens=200, flint_tokens_in=3000, flint_tokens_out=70,
            duration_ms=4000),
        row("flint_write", "applied", report_tokens=300, duration_ms=10000),
        row("flint_write", "applied", report_tokens=300, duration_ms=20000),
        row("flint_write", "rejected", report_tokens=300),
        row("flint_write", "miss"),
        row("flint_write", "check_failed", days_ago=10),
    ]  # fmt: skip
    path = tmp_path / "log.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_accept_rate_is_applied_over_all_decided_outcomes(log_path):
    summary = stats.summarize(stats.load_rows(log_path))
    # 2 applied / (2 applied + 1 rejected + 1 miss + 1 check_failed)
    assert summary["accept_rate"] == pytest.approx(0.4)


def test_counts_per_tool_and_outcome(log_path):
    summary = stats.summarize(stats.load_rows(log_path))
    assert summary["calls"] == {"flint_read": 2, "flint_write": 5}
    assert summary["outcomes"]["applied"] == 2
    assert summary["outcomes"]["ok"] == 2


def test_averages_ignore_missing_values(log_path):
    summary = stats.summarize(stats.load_rows(log_path))
    assert summary["avg_report_tokens"] == pytest.approx(240)  # 100+200+300*3 over 5 rows
    assert summary["avg_duration_ms"] == {"flint_read": 3000, "flint_write": 15000}
    assert summary["tokens_in"] == 4000
    assert summary["tokens_out"] == 120


def test_accept_rate_is_none_when_nothing_was_decided(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text(json.dumps(row("flint_read", "ok")) + "\n")
    assert stats.summarize(stats.load_rows(path))["accept_rate"] is None


def test_since_filters_old_rows(log_path):
    assert len(stats.load_rows(log_path)) == 7
    assert len(stats.load_rows(log_path, since="7d")) == 6
    assert len(stats.load_rows(log_path, since="24h")) == 6


def test_since_rejects_garbage():
    with pytest.raises(ValueError):
        stats.parse_since("a week")


def test_main_prints_the_summary(log_path, capsys):
    stats.main(["--log", str(log_path), "--since", "7d"])
    out = capsys.readouterr().out
    assert "flint_write" in out
    assert "accept rate" in out.lower()
    assert "50.0%" in out  # the 10-day-old check_failed is filtered out


def test_main_with_a_missing_log_says_so(tmp_path, capsys):
    assert stats.main(["--log", str(tmp_path / "nope.jsonl")]) == 1
    assert "no log" in capsys.readouterr().out.lower()
