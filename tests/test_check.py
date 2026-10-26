import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from puppet_master import gitops, server
from puppet_master.check import extract_failures, run_check
from puppet_master.config import Config

SRC = Path(__file__).parent.parent / "src"
PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]


def cfg_for(**commands) -> Config:
    return replace(Config(), check_commands=commands or {"test": PYTEST})


def make_project(tmp_path: Path, passing: bool) -> Path:
    (tmp_path / "test_sample.py").write_text(f"def test_it():\n    assert {passing}\n")
    return tmp_path


def test_unlisted_command_names_are_refused_and_nothing_runs(tmp_path):
    marker = tmp_path / "ran"
    cfg = cfg_for(test=PYTEST)
    for cmd in ("rm -rf /", "test; ls", f"touch {marker}", ""):
        out = run_check(cfg, tmp_path, cmd)
        assert out == {"error": "not_allowed", "allowed": ["test"]}
    assert not marker.exists()


def test_passing_pytest_reports_no_failures(tmp_path):
    out = run_check(cfg_for(), make_project(tmp_path, True), "test")
    assert out == {"passed": True, "exit_code": 0, "failures": []}


def test_failing_pytest_reports_the_failed_lines(tmp_path):
    out = run_check(cfg_for(), make_project(tmp_path, False), "test")
    assert out["passed"] is False
    assert out["exit_code"] == 1
    assert any(f.startswith("FAILED test_sample.py::test_it") for f in out["failures"])


def test_timeout_is_a_failed_check(tmp_path):
    cfg = replace(
        cfg_for(slow=[sys.executable, "-c", "import time; time.sleep(5)"]), check_timeout_s=1
    )
    out = run_check(cfg, tmp_path, "slow")
    assert out == {"passed": False, "exit_code": None, "failures": ["timeout"]}


def test_missing_binary_is_a_failed_check(tmp_path):
    out = run_check(cfg_for(test=["definitely-not-a-real-binary"]), tmp_path, "test")
    assert out["passed"] is False
    assert out["exit_code"] is None
    assert out["failures"] == ["command_not_found"]


def test_extract_failures_picks_failed_error_and_lint_lines():
    output = "\n".join(
        [
            "collected 3 items",
            "FAILED tests/test_a.py::test_x - assert 1 == 2",
            "ERROR tests/test_b.py - ImportError",
            "src/a.py:3:1: F401 `os` imported but unused",
            "noise line",
        ]
    )
    assert extract_failures(output, 1) == [
        "FAILED tests/test_a.py::test_x - assert 1 == 2",
        "ERROR tests/test_b.py - ImportError",
        "src/a.py:3:1: F401 `os` imported but unused",
    ]


def test_extract_failures_falls_back_to_the_tail_on_a_nonzero_exit():
    output = "\n".join(f"line {i}" for i in range(50))
    got = extract_failures(output, 2)
    assert got == [f"line {i}" for i in range(30, 50)]
    assert extract_failures(output, 0) == []


def test_extract_failures_caps_count_and_length():
    output = "\n".join(f"FAILED t{i} " + "x" * 400 for i in range(40))
    got = extract_failures(output, 1)
    assert len(got) == 20
    assert all(len(f) <= 200 for f in got)


def test_no_shell_true_anywhere_in_src():
    offenders = [str(p) for p in SRC.rglob("*.py") if "shell=True" in p.read_text()]
    assert offenders == []


def test_run_git_returns_stdout_and_raises_on_failure(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert gitops.run_git(tmp_path, "rev-parse", "--is-inside-work-tree").strip() == "true"
    with pytest.raises(gitops.GitError):
        gitops.run_git(tmp_path, "rev-parse", "--verify", "nope")


async def test_flint_check_tool_runs_in_root_and_rejects_bad_names(tmp_path):
    (tmp_path / "puppet-master.toml").write_text(
        f"[check.commands]\ntest = {list(PYTEST)!r}\n".replace("'", '"')
    )
    make_project(tmp_path, False)
    bad = await server.puppet_check("test; ls", str(tmp_path))
    assert bad["error"] == "not_allowed"
    out = await server.puppet_check("test", str(tmp_path))
    assert out["passed"] is False
    assert out["failures"]


async def test_flint_check_needs_an_absolute_root():
    assert (await server.puppet_check("test", "relative/dir"))["error"] == "relative_path"
