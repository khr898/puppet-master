import json
from datetime import UTC, datetime, timedelta

import pytest

from puppet_master import stats


def row(tool, outcome, *, days_ago=0.0, **fields) -> dict:
    ts = datetime.now(UTC) - timedelta(days=days_ago)
    return {"ts": ts.isoformat(), "tool": tool, "outcome": outcome, **fields}


@pytest.fixture
def log_path(tmp_path):
    rows = [
        row("puppet_read", "ok", report_tokens=100, flint_tokens_in=1000, flint_tokens_out=50,
            duration_ms=2000),
        row("puppet_read", "ok", report_tokens=200, flint_tokens_in=3000, flint_tokens_out=70,
            duration_ms=4000),
        row("puppet_write", "applied", report_tokens=300, duration_ms=10000),
        row("puppet_write", "applied", report_tokens=300, duration_ms=20000),
        row("puppet_write", "rejected", report_tokens=300),
        row("puppet_write", "miss"),
        row("puppet_write", "check_failed", days_ago=10),
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
    assert summary["calls"] == {"puppet_read": 2, "puppet_write": 5}
    assert summary["outcomes"]["applied"] == 2
    assert summary["outcomes"]["ok"] == 2


def test_averages_ignore_missing_values(log_path):
    summary = stats.summarize(stats.load_rows(log_path))
    assert summary["avg_report_tokens"] == pytest.approx(240)  # 100+200+300*3 over 5 rows
    assert summary["avg_duration_ms"] == {"puppet_read": 3000, "puppet_write": 15000}
    assert summary["tokens_in"] == 4000
    assert summary["tokens_out"] == 120


def test_accept_rate_is_none_when_nothing_was_decided(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text(json.dumps(row("puppet_read", "ok")) + "\n")
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
    assert "puppet_write" in out
    assert "accept rate" in out.lower()
    assert "50.0%" in out  # the 10-day-old check_failed is filtered out


def test_main_with_a_missing_log_says_so(tmp_path, capsys):
    assert stats.main(["--log", str(tmp_path / "nope.jsonl")]) == 1
    assert "no log" in capsys.readouterr().out.lower()


def test_a_damaged_log_line_is_skipped(tmp_path):
    path = tmp_path / "log.jsonl"
    good = json.dumps(row("puppet_read", "ok"))
    path.write_text(f'{good}\n{{"ts": "2026-\n{json.dumps({"tool": "x"})}\n[1]\n{good}\n')
    assert len(stats.load_rows(path)) == 2
    assert len(stats.load_rows(path, since="7d")) == 2


def measured_log(tmp_path):
    rows = [
        row("puppet_status", "ok", cost_in=60, cost_out=1),
        row("puppet_read", "ok", cost_in=150, cost_out=20, avoided_in=3000, claims=4, stripped=1),
        row("puppet_read", "error", cost_in=10, cost_out=20, error="unverifiable_output"),
        row("puppet_write", "staged", cost_in=400, cost_out=50),
        row("puppet_apply", "applied", cost_in=20, cost_out=10, avoided_in=2000, avoided_out=300),
        row("puppet_ask", "ok", cost_in=100, cost_out=40, avoided_out=200),
        row("puppet_read", "ok", report_tokens=90),  # logged before costs were measured
    ]  # fmt: skip
    path = tmp_path / "log.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return stats.summarize(stats.load_rows(path))


def test_savings_are_avoided_minus_cost_over_measured_rows(tmp_path):
    s = measured_log(tmp_path)["savings"]
    assert s["measured"] == 6 and s["unmeasured"] == 1
    assert (s["avoided_in"], s["avoided_out"]) == (5000, 500)
    assert (s["cost_in"], s["cost_out"]) == (740, 141)
    assert (s["net_in"], s["net_out"]) == (4260, 359)
    assert s["net_weighted"] == 4260 + 5 * 359
    assert s["saved_pct"] == pytest.approx(100 * (4260 + 5 * 359) / (5000 + 5 * 500))
    assert s["per_tool"]["puppet_read"] == {"cost": 150 + 100 + 10 + 100, "avoided": 3000}


def test_accuracy_counts_kept_and_stripped_claims_and_unverifiable_output(tmp_path):
    a = measured_log(tmp_path)["accuracy"]
    assert (a["claims_kept"], a["claims_stripped"]) == (4, 1)
    assert a["strip_rate"] == pytest.approx(0.2)
    assert a["unverifiable"] == 1


def test_main_prints_savings_and_accuracy(tmp_path, capsys):
    measured_log(tmp_path)
    stats.main(["--log", str(tmp_path / "log.jsonl")])
    out = capsys.readouterr().out
    assert "Claude tokens saved" in out
    assert "6055" in out  # net weighted
    assert "1 call logged before costs were measured" in out
    assert "Claims stripped: 1 of 5 (20.0%)" in out
