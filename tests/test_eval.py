import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import respx

from flint_bridge import eval as flint_eval
from flint_bridge import lifecycle, server
from flint_bridge.config import Config
from flint_bridge.read import Usage

ROOT = Path(__file__).parent.parent
EVALS = ROOT / "evals"
BASE = Config().base_url
URL = f"{BASE}/v1/chat/completions"
READ_TASKS = json.loads((EVALS / "read_tasks.json").read_text())
WRITE_TASKS = json.loads((EVALS / "write_tasks.json").read_text())


# --- canned model replies ------------------------------------------------------------------


def chat(content: dict | str) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content)
    body = {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 500, "completion_tokens": 100},
    }
    return httpx.Response(200, json=body)


def first_numbered_line(user: str) -> tuple[str, int, str]:
    """(file, line number, text) of the first non-empty line the model was shown."""
    path = re.search(r"=== FILE (.*) ===", user).group(1)
    m = re.search(r"^\s*(\d+)\| (\S.*)$", user, re.M)
    return path, int(m.group(1)), m.group(2)


def read_reply(user: str, *, bogus: int = 0) -> dict:
    question = user.rsplit("QUESTION: ", 1)[1]
    task = next(t for t in READ_TASKS if t["question"] == question)
    file, line, text = first_numbered_line(user)
    claims = [{"text": "real", "file": file, "line": line, "quote": text}]
    claims += [
        {"text": "made up", "file": file, "line": line, "quote": f"no such text {i}"}
        for i in range(bogus)
    ]
    return {"answer": " ".join(task["must_mention"]), "claims": claims}


def write_reply(user: str) -> dict:
    task_text = user.rsplit("TASK: ", 1)[1].split("\nCONSTRAINTS:")[0]
    task = next(t for t in WRITE_TASKS if t["task"] == task_text)
    name = task["files"][0]
    path = re.search(r"=== FILE (.*) ===", user).group(1)
    solution = (EVALS / "fixtures" / task["fixture"] / "solution" / name).read_text()
    return {"files": [{"path": path, "content": solution}], "notes": "fixed"}


def model(*, bogus: int = 0, broken: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        system, user = messages[0]["content"], messages[1]["content"]
        if broken:
            return chat("this is not json")
        if "edit source files" in system:
            return chat(write_reply(user))
        return chat(read_reply(user, bogus=bogus))

    return handler


@pytest.fixture(autouse=True)
def flint_up(monkeypatch):
    monkeypatch.setattr(server, "mem_pressure", lambda: 10)
    with respx.mock:
        respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json={}))
        respx.get(f"{BASE}/props").mock(
            return_value=httpx.Response(200, json={"default_generation_settings": {"n_ctx": 16384}})
        )
        yield


# --- the task set itself -------------------------------------------------------------------


def test_the_task_set_has_ten_read_and_five_write_tasks():
    assert len(READ_TASKS) == 10
    assert len(WRITE_TASKS) == 5


@pytest.mark.parametrize("task", READ_TASKS, ids=lambda t: t["id"])
def test_every_read_task_points_at_source_that_contains_its_keywords(task):
    text = ""
    for p in task["paths"]:
        path = ROOT / re.sub(r":\d+-\d+$", "", p)
        assert path.exists(), p
        text += path.read_text().lower()
    for keyword in task["must_mention"]:
        assert keyword.lower() in text, f"{keyword!r} not in the source of {task['id']}"


def run_pytest(folder: Path) -> int:
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x"]
    return subprocess.run(argv, cwd=folder, capture_output=True).returncode


@pytest.mark.parametrize("task", WRITE_TASKS, ids=lambda t: t["id"])
def test_every_write_fixture_fails_as_shipped_and_passes_with_its_solution(task, tmp_path):
    fixture = EVALS / "fixtures" / task["fixture"]
    work = tmp_path / "work"
    shutil.copytree(fixture, work, ignore=shutil.ignore_patterns("solution", "__pycache__"))
    assert run_pytest(work) != 0
    for f in (fixture / "solution").glob("*.py"):
        shutil.copy(f, work / f.name)
    assert run_pytest(work) == 0


# --- scoring -------------------------------------------------------------------------------


def test_answer_correct_needs_every_keyword_case_insensitively():
    assert flint_eval.answer_correct("It uses CEIL of len/4", ["ceil", "4"])
    assert not flint_eval.answer_correct("It uses ceil", ["ceil", "4"])


async def test_perfect_answers_score_one_hundred_percent():
    respx.post(URL).mock(side_effect=model())
    res = await flint_eval.run_eval(Config(), EVALS, pid_fn=lambda: None)
    summary = res["summary"]
    assert summary["read"]["correct_pct"] == 100
    assert summary["read"]["avg_claims_kept"] == 1
    assert summary["read"]["avg_stripped"] == 0
    assert summary["read"]["unverifiable"] == 0
    assert summary["write"]["check_pass_pct"] == 100
    assert summary["speed"]["tok_per_s"] is not None
    assert summary["memory"]["peak_rss_gb"] is None
    assert len(res["reads"]) == 10 and len(res["writes"]) == 5


async def test_hallucinated_claims_show_up_as_stripped():
    respx.post(URL).mock(side_effect=model(bogus=2))
    res = await flint_eval.run_eval(Config(), EVALS, pid_fn=lambda: None)
    assert res["summary"]["read"]["avg_stripped"] == 2
    assert all(r["stripped"] == 2 for r in res["reads"])
    assert res["summary"]["read"]["avg_claims_kept"] == 1


async def test_unusable_output_is_counted_as_unverifiable_and_wrong():
    respx.post(URL).mock(side_effect=model(broken=True))
    res = await flint_eval.run_eval(Config(), EVALS, pid_fn=lambda: None)
    assert res["summary"]["read"]["unverifiable"] == 10
    assert res["summary"]["read"]["correct_pct"] == 0
    assert res["summary"]["write"]["check_pass_pct"] == 0


async def test_the_real_tree_and_evals_are_left_alone():
    respx.post(URL).mock(side_effect=model())
    before = subprocess.run(
        ["git", "status", "--porcelain", "evals"], cwd=ROOT, capture_output=True, text=True
    ).stdout
    await flint_eval.run_eval(Config(), EVALS, pid_fn=lambda: None)
    after = subprocess.run(
        ["git", "status", "--porcelain", "evals"], cwd=ROOT, capture_output=True, text=True
    ).stdout
    assert before == after


def test_usage_tracks_model_seconds():
    assert Usage().model_seconds == 0.0


# --- memory --------------------------------------------------------------------------------


async def test_peak_rss_keeps_the_highest_reading(monkeypatch):
    readings = iter([8.0, 11.5, 9.0])
    monkeypatch.setattr(lifecycle, "rss_gb", lambda pid: next(readings))
    peak = flint_eval.RssPeak(lambda: 123, interval=0)
    for _ in range(3):
        peak.sample()
    assert peak.peak == 11.5


def test_peak_rss_is_none_without_a_managed_server():
    peak = flint_eval.RssPeak(lambda: None, interval=0)
    peak.sample()
    assert peak.peak is None


# --- the CLI -------------------------------------------------------------------------------


async def test_main_writes_results_and_compare_prints_both_labels(tmp_path, capsys):
    respx.post(URL).mock(side_effect=model())
    results = tmp_path / "results"
    argv = ["--evals-dir", str(EVALS), "--results-dir", str(results)]
    for label in ("flint-a", "gemma-b"):
        assert await flint_eval.amain([*argv, "--label", label]) == 0
        assert (results / f"{label}.json").exists()
    capsys.readouterr()

    assert await flint_eval.amain([*argv, "--compare", "flint-a", "gemma-b"]) == 0
    out = capsys.readouterr().out
    assert "flint-a" in out and "gemma-b" in out
    assert "check-pass" in out.lower()


async def test_compare_with_a_missing_label_says_so(tmp_path, capsys):
    argv = ["--evals-dir", str(EVALS), "--results-dir", str(tmp_path)]
    assert await flint_eval.amain([*argv, "--compare", "nope", "never"]) == 1
    assert "nope" in capsys.readouterr().out


def test_main_needs_a_label_or_compare():
    with pytest.raises(SystemExit):
        flint_eval.main([])
