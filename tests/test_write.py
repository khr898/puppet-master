import hashlib
import json
import subprocess
from pathlib import Path

import httpx
import pytest
import respx

from flint_bridge import server
from flint_bridge.config import Config, load_config
from flint_bridge.gitops import run_git
from flint_bridge.write import SYSTEM_WRITE, flint_write_impl, task_key

URL = f"{Config().base_url}/v1/chat/completions"


def llm_reply(content: str | dict) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content)
    body = {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 800, "completion_tokens": 120},
    }
    return httpx.Response(200, json=body)


def edit(path: Path, content: str, notes: str = "done") -> dict:
    return {"files": [{"path": str(path), "content": content}], "notes": notes}


def status(root: Path) -> str:
    return run_git(root, "status", "--porcelain")


def staged(root: Path) -> list[Path]:
    d = root / ".flint" / "staging"
    return sorted(d.glob("*")) if d.exists() else []


def user_message(route, call: int = 0) -> str:
    return json.loads(route.calls[call].request.content)["messages"][1]["content"]


ADD_AND_SUB = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"


@respx.mock
async def test_valid_edit_is_staged_with_a_hashed_patch_and_the_tree_is_untouched(git_repo):
    app = git_repo / "app.py"
    route = respx.post(URL).mock(return_value=llm_reply(edit(app, ADD_AND_SUB, "added sub")))
    before = status(git_repo)

    out = await flint_write_impl(load_config(git_repo), "add a sub function", [str(app)], "")

    assert out["check"] == {"passed": True, "failures": []}
    assert out["notes"] == "added sub"
    assert "app.py" in out["diffstat"]
    patch = Path(out["patch_path"])
    assert patch == git_repo.resolve() / ".flint" / "staging" / f"{out['patch_id']}.patch"
    assert "+def sub(a, b):" in patch.read_text()
    meta = json.loads(patch.with_suffix(".json").read_text())
    assert meta["sha256"] == hashlib.sha256(patch.read_bytes()).hexdigest()
    assert meta["patch_id"] == out["patch_id"]
    assert meta["task_key"] == task_key("add a sub function")
    assert meta["files"] == ["app.py"]
    assert meta["applied"] is False and meta["check"]["passed"] is True
    assert status(git_repo) == before
    assert app.read_text().count("def sub") == 0
    assert route.call_count == 1


@respx.mock
async def test_prompt_puts_files_first_and_the_task_last(git_repo):
    app, new = git_repo / "app.py", git_repo / "extra.py"
    route = respx.post(URL).mock(return_value=llm_reply(edit(app, ADD_AND_SUB)))
    await flint_write_impl(load_config(git_repo), "do it", [str(new), str(app)], "keep style")
    body = json.loads(route.calls[0].request.content)
    msg = body["messages"][1]["content"]
    assert body["messages"][0]["content"] == SYSTEM_WRITE
    assert body["response_format"]["json_schema"]["schema"]["required"] == ["files", "notes"]
    assert msg.index(f"=== FILE {app.resolve()} ===") < msg.index(f"=== FILE {new.resolve()} ===")
    assert "(new file)" in msg
    assert "def add(a, b):" in msg  # raw content, no line-number margin
    assert msg.index("TASK") > msg.rindex("=== FILE")
    assert msg.rstrip().endswith("keep style")


@respx.mock
async def test_a_breaking_edit_is_staged_but_reports_the_failed_check(git_repo):
    app = git_repo / "app.py"
    respx.post(URL).mock(return_value=llm_reply(edit(app, "def add(a, b):\n    return a - b\n")))
    out = await flint_write_impl(load_config(git_repo), "break it", [str(app)], "")
    assert out["check"]["passed"] is False
    assert any("test_add" in f for f in out["check"]["failures"])
    assert len(staged(git_repo)) == 2
    assert json.loads((git_repo / ".flint" / "attempts.json").read_text()) == {
        task_key("break it"): {"failed": 1}
    }


@respx.mock
async def test_a_path_outside_files_is_rejected_and_nothing_is_staged(git_repo):
    app, other = git_repo / "app.py", git_repo / "test_app.py"
    reply = {
        "files": [
            {"path": str(app), "content": ADD_AND_SUB},
            {"path": str(other), "content": "pass\n"},
        ],
        "notes": "",
    }
    respx.post(URL).mock(return_value=llm_reply(reply))
    out = await flint_write_impl(load_config(git_repo), "t", [str(app)], "")
    assert out["error"] == "unexpected_path"
    assert staged(git_repo) == []
    assert not (git_repo / ".flint" / "attempts.json").exists()


@respx.mock
async def test_oversized_file_and_too_many_files_are_over_budget_without_a_call(git_repo):
    route = respx.post(URL).mock(return_value=llm_reply(edit(git_repo / "app.py", "x")))
    big = git_repo / "big.py"
    big.write_text("x = 1\n" * 5000)  # 30 KB
    out = await flint_write_impl(load_config(git_repo), "t", [str(big)], "")
    assert out["error"] == "over_budget"
    assert "one task per file" in out["hint"]
    four = [str(git_repo / f"f{i}.py") for i in range(4)]
    assert (await flint_write_impl(load_config(git_repo), "t", four, ""))["error"] == "over_budget"
    assert route.call_count == 0


@respx.mock
async def test_total_size_across_files_is_budgeted_too(git_repo):
    route = respx.post(URL).mock(return_value=llm_reply(edit(git_repo / "app.py", "x")))
    paths = []
    for i in range(3):
        p = git_repo / f"m{i}.py"
        p.write_text("y = 1\n" * 1500)  # 9 KB ≈ 2.2k tokens each: 6.7k together
        paths.append(str(p))
    out = await flint_write_impl(load_config(git_repo), "t", paths, "")
    assert out["error"] == "over_budget"
    assert route.call_count == 0


@respx.mock
async def test_a_new_file_path_produces_a_patch_that_creates_it(git_repo):
    new = git_repo / "pkg" / "util.py"
    respx.post(URL).mock(return_value=llm_reply(edit(new, "VALUE = 3\n")))
    out = await flint_write_impl(load_config(git_repo), "make util", [str(new)], "")
    patch = Path(out["patch_path"]).read_text()
    assert "new file mode" in patch and "+VALUE = 3" in patch
    assert not new.exists()
    assert out["check"]["passed"] is True


@respx.mock
async def test_invalid_json_twice_is_unverifiable_and_stages_nothing(git_repo):
    route = respx.post(URL).mock(return_value=llm_reply("not json"))
    out = await flint_write_impl(load_config(git_repo), "t", [str(git_repo / "app.py")], "")
    assert out == {"error": "unverifiable_output"}
    assert route.call_count == 2
    assert staged(git_repo) == []


@respx.mock
async def test_identical_content_is_no_changes(git_repo):
    app = git_repo / "app.py"
    respx.post(URL).mock(return_value=llm_reply(edit(app, app.read_text())))
    out = await flint_write_impl(load_config(git_repo), "t", [str(app)], "")
    assert out == {"error": "no_changes"}
    assert staged(git_repo) == []


async def test_files_must_be_absolute_and_inside_one_repo(git_repo, tmp_path):
    cfg = load_config(git_repo)
    assert (await flint_write_impl(cfg, "t", ["app.py"], ""))["error"] == "relative_path"
    assert (await flint_write_impl(cfg, "t", [], ""))["error"] == "no_files"
    outside = tmp_path / "loose"
    outside.mkdir()
    assert (await flint_write_impl(cfg, "t", [str(outside / "a.py")], ""))["error"] == "not_in_repo"
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    out = await flint_write_impl(cfg, "t", [str(git_repo / "app.py"), str(other / "b.py")], "")
    assert out["error"] == "multiple_repos"


def test_task_key_ignores_case_and_whitespace():
    assert task_key("Add  a\nfunction ") == task_key("add a function")
    assert task_key("add a function") != task_key("add another")
    assert len(task_key("x")) == 12


# --- the MCP tool -------------------------------------------------------------------------


@pytest.fixture
def tool_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(server, "mem_pressure", lambda: 10)
    respx.get(f"{Config().base_url}/health").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{Config().base_url}/props").mock(
        return_value=httpx.Response(200, json={"default_generation_settings": {"n_ctx": 16384}})
    )


def log_rows(root: Path) -> list[dict]:
    return [json.loads(x) for x in (root / ".flint" / "log.jsonl").read_text().splitlines()]


@respx.mock
async def test_tool_logs_staged_and_check_failed_with_the_task_key(tool_env, git_repo):
    app = git_repo / "app.py"
    respx.post(URL).mock(
        side_effect=[
            llm_reply(edit(app, ADD_AND_SUB)),
            llm_reply(edit(app, "def add(a, b):\n    return 0\n")),
        ]
    )
    good = await server.flint_write("grow app", [str(app)])
    bad = await server.flint_write("shrink app", [str(app)])
    assert good["check"]["passed"] and not bad["check"]["passed"]
    rows = log_rows(git_repo)
    assert [r["outcome"] for r in rows] == ["staged", "check_failed"]
    assert rows[0]["task_key"] == task_key("grow app")
    assert rows[0]["flint_tokens_in"] == 800 and rows[0]["tool"] == "flint_write"


@respx.mock
async def test_tool_report_stays_within_500_tokens(tool_env, git_repo):
    from flint_bridge.report import est_tokens

    app = git_repo / "app.py"
    respx.post(URL).mock(return_value=llm_reply(edit(app, ADD_AND_SUB, "note " * 1000)))
    out = await server.flint_write("grow app", [str(app)])
    assert est_tokens(json.dumps(out)) <= 500
    assert out["patch_id"] and out["patch_path"]
    assert len(out["notes"]) <= 300


@respx.mock
async def test_tool_returns_flint_offline(tool_env, git_repo):
    respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
    out = await server.flint_write("t", [str(git_repo / "app.py")])
    assert out == {"error": "flint_offline"}
    assert log_rows(git_repo)[0]["outcome"] == "offline"


async def test_tool_description_states_the_contract():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    desc = tools["flint_write"].description
    assert "STAGED patch" in desc and "MUST read the patch" in desc and "≤3 files" in desc


@respx.mock
async def test_tool_report_with_many_long_failures_stays_within_500_tokens(
    tool_env, git_repo, monkeypatch
):
    from flint_bridge import write
    from flint_bridge.report import est_tokens

    check = {"passed": False, "exit_code": 1, "failures": ["FAILED t " + "x" * 190] * 20}
    fake = {
        "patch": "diff\n",
        "diffstat": " app.py | 2 +-\n" * 3,
        "files": ["app.py"],
        "check": check,
    }
    monkeypatch.setattr(write, "stage_and_check", lambda *_a, **_k: fake)
    app = git_repo / "app.py"
    respx.post(URL).mock(return_value=llm_reply(edit(app, ADD_AND_SUB, "n" * 400)))
    out = await server.flint_write("grow app", [str(app)])
    assert est_tokens(json.dumps(out)) <= 500
    assert out["check"]["passed"] is False and out["check"]["failures"]
    assert out["patch_id"] and out["patch_path"]


@respx.mock
async def test_project_without_a_configured_check_never_calls_flint(git_repo):
    (git_repo / "flint.toml").unlink()
    route = respx.post(URL).mock(return_value=llm_reply(edit(git_repo / "app.py", "x")))
    out = await flint_write_impl(load_config(git_repo), "t", [str(git_repo / "app.py")], "")
    assert out["error"] == "no_check_configured"
    assert route.call_count == 0


def test_stage_and_check_lists_only_files_that_changed(git_repo):
    from flint_bridge.gitops import stage_and_check

    cfg = load_config(git_repo)
    new = {
        git_repo / "app.py": ADD_AND_SUB,
        git_repo / "test_app.py": (git_repo / "test_app.py").read_text(),
    }
    out = stage_and_check(cfg, git_repo, new, "test")
    assert out["files"] == ["app.py"]


def test_flint_state_is_not_copied_into_the_worktree(git_repo):
    from flint_bridge.gitops import baseline_worktree

    (git_repo / ".gitignore").write_text("")  # .flint is untracked, not ignored
    (git_repo / ".flint" / "staging").mkdir(parents=True)
    (git_repo / ".flint" / "staging" / "abc.patch").write_text("x")
    with baseline_worktree(git_repo) as tmp:
        assert not (tmp / ".flint").exists()
