import json
from pathlib import Path

import httpx
import pytest
import respx

from flint_bridge import attempts, server
from flint_bridge.config import Config, load_config
from flint_bridge.gitops import run_git
from flint_bridge.write import flint_apply_impl, flint_reject_impl, flint_write_impl, task_key

URL = f"{Config().base_url}/v1/chat/completions"
GOOD = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
BAD = "def add(a, b):\n    return a - b\n"


def llm_reply(path: Path, content: str) -> httpx.Response:
    reply = {"files": [{"path": str(path), "content": content}], "notes": "n"}
    body = {"choices": [{"message": {"content": json.dumps(reply)}}], "usage": {}}
    return httpx.Response(200, json=body)


def status(root: Path) -> str:
    return run_git(root, "status", "--porcelain")


async def stage(repo: Path, content: str, task: str = "edit app") -> dict:
    """Stage an edit of app.py through the real flint_write pipeline (LLM mocked)."""
    app = repo / "app.py"
    with respx.mock:
        respx.post(URL).mock(return_value=llm_reply(app, content))
        return await flint_write_impl(load_config(repo), task, [str(app)], "")


def meta(repo: Path, patch_id: str) -> dict:
    return json.loads((repo / ".flint" / "staging" / f"{patch_id}.json").read_text())


async def test_happy_path_applies_once_and_clears_the_attempt_count(git_repo):
    attempts.record(git_repo, task_key("edit app"), passed=False)
    staged = await stage(git_repo, GOOD)
    assert staged["check"]["passed"]
    out = flint_apply_impl(git_repo, staged["patch_id"])
    assert out == {"applied": True, "files": ["app.py"]}
    assert (git_repo / "app.py").read_text() == GOOD
    assert meta(git_repo, staged["patch_id"])["applied"] is True
    assert not attempts.give_up(git_repo, task_key("edit app"))
    assert task_key("edit app") not in json.loads(
        (git_repo / ".flint" / "attempts.json").read_text()
    )

    before = status(git_repo)
    assert flint_apply_impl(git_repo, staged["patch_id"]) == {"error": "already_applied"}
    assert status(git_repo) == before


async def test_unknown_and_malformed_patch_ids_are_refused(git_repo):
    before = status(git_repo)
    assert flint_apply_impl(git_repo, "abc123") == {"error": "unknown_patch"}
    assert flint_apply_impl(git_repo, "../../etc/passwd") == {"error": "invalid_patch_id"}
    assert flint_apply_impl(Path("relative"), "abc123")["error"] == "relative_path"
    assert status(git_repo) == before


async def test_a_failed_check_blocks_apply_and_leaves_the_tree_alone(git_repo):
    staged = await stage(git_repo, BAD)
    assert staged["check"]["passed"] is False
    before = status(git_repo)
    assert flint_apply_impl(git_repo, staged["patch_id"]) == {"error": "check_failed"}
    assert status(git_repo) == before
    assert (git_repo / "app.py").read_text().count("a + b") == 1


async def test_a_patch_edited_after_its_check_is_refused(git_repo):
    staged = await stage(git_repo, GOOD)
    patch = Path(staged["patch_path"])
    patch.write_text(patch.read_text().replace("a - b", "a * b"))
    before = status(git_repo)
    assert flint_apply_impl(git_repo, staged["patch_id"]) == {"error": "patch_modified"}
    assert status(git_repo) == before
    assert not meta(git_repo, staged["patch_id"])["applied"]


async def test_flipping_check_passed_in_the_metadata_does_not_unlock_apply(git_repo):
    staged = await stage(git_repo, BAD)
    path = git_repo / ".flint" / "staging" / f"{staged['patch_id']}.json"
    data = json.loads(path.read_text())
    data["check"]["passed"] = True
    path.write_text(json.dumps(data))
    before = status(git_repo)
    assert flint_apply_impl(git_repo, staged["patch_id"]) == {"error": "metadata_modified"}
    assert status(git_repo) == before


async def test_a_swapped_hash_in_the_metadata_is_refused(git_repo):
    staged = await stage(git_repo, GOOD)
    patch = Path(staged["patch_path"])
    patch.write_text(patch.read_text().replace("a - b", "a * b"))
    import hashlib

    path = patch.with_suffix(".json")
    data = json.loads(path.read_text())
    data["sha256"] = hashlib.sha256(patch.read_bytes()).hexdigest()
    path.write_text(json.dumps(data))
    assert flint_apply_impl(git_repo, staged["patch_id"]) == {"error": "metadata_modified"}


async def test_a_missing_patch_file_is_refused(git_repo):
    staged = await stage(git_repo, GOOD)
    Path(staged["patch_path"]).unlink()
    assert flint_apply_impl(git_repo, staged["patch_id"]) == {"error": "unknown_patch"}


async def test_a_patch_that_no_longer_fits_the_tree_is_refused(git_repo):
    staged = await stage(git_repo, GOOD)
    (git_repo / "app.py").write_text("def add(a, b):\n    return a + b + 0\n")
    before = status(git_repo)
    out = flint_apply_impl(git_repo, staged["patch_id"])
    assert out["error"] == "does_not_apply"
    assert 0 < len(out["detail"]) <= 300
    assert status(git_repo) == before
    assert not meta(git_repo, staged["patch_id"])["applied"]


async def test_corrupt_metadata_is_unknown(git_repo):
    staged = await stage(git_repo, GOOD)
    (git_repo / ".flint" / "staging" / f"{staged['patch_id']}.json").write_text("{nope")
    assert flint_apply_impl(git_repo, staged["patch_id"]) == {"error": "unknown_patch"}


# --- flint_reject -------------------------------------------------------------------------


async def test_rejecting_a_passing_patch_counts_a_failed_attempt_and_deletes_it(git_repo):
    staged = await stage(git_repo, GOOD)
    out = flint_reject_impl(git_repo, staged["patch_id"], "style")
    assert out == {"rejected": True}
    assert list((git_repo / ".flint" / "staging").glob("*")) == []
    data = json.loads((git_repo / ".flint" / "attempts.json").read_text())
    assert data[task_key("edit app")] == {"failed": 1}
    assert flint_apply_impl(git_repo, staged["patch_id"]) == {"error": "unknown_patch"}


async def test_rejecting_a_patch_that_failed_its_check_is_not_counted_twice(git_repo):
    staged = await stage(git_repo, BAD)  # already counted when written
    flint_reject_impl(git_repo, staged["patch_id"], "broken")
    data = json.loads((git_repo / ".flint" / "attempts.json").read_text())
    assert data[task_key("edit app")] == {"failed": 1}


async def test_reject_refuses_unknown_and_applied_patches(git_repo):
    assert flint_reject_impl(git_repo, "abc123", "x") == {"error": "unknown_patch"}
    assert flint_reject_impl(git_repo, "../x", "x") == {"error": "invalid_patch_id"}
    staged = await stage(git_repo, GOOD)
    flint_apply_impl(git_repo, staged["patch_id"])
    assert flint_reject_impl(git_repo, staged["patch_id"], "x") == {"error": "already_applied"}


# --- the MCP tools ------------------------------------------------------------------------


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
async def test_two_failed_attempts_then_give_up_without_calling_flint(tool_env, git_repo):
    app = git_repo / "app.py"
    route = respx.post(URL).mock(return_value=llm_reply(app, BAD))
    first = await server.flint_write("fix add", [str(app)])
    second = await server.flint_write("Fix  add", [str(app)], "try harder")
    assert first["check"]["passed"] is False and second["check"]["passed"] is False
    assert route.call_count == 2

    third = await server.flint_write("fix add", [str(app)], "once more")
    assert third == {"give_up": True, "task_key": task_key("fix add")}
    assert route.call_count == 2
    rows = log_rows(git_repo)
    assert [r["outcome"] for r in rows] == ["check_failed", "check_failed", "miss"]
    assert rows[2]["task_key"] == task_key("fix add")


@respx.mock
async def test_two_rejections_also_end_in_a_miss(tool_env, git_repo):
    app = git_repo / "app.py"
    route = respx.post(URL).mock(return_value=llm_reply(app, GOOD))
    for _ in range(2):
        out = await server.flint_write("grow", [str(app)])
        await server.flint_reject(str(git_repo), out["patch_id"], "not what I wanted")
    assert (await server.flint_write("grow", [str(app)]))["give_up"] is True
    assert route.call_count == 2
    assert [r["outcome"] for r in log_rows(git_repo)] == [
        "staged", "rejected", "staged", "rejected", "miss",
    ]  # fmt: skip


@respx.mock
async def test_apply_tool_applies_and_logs(tool_env, git_repo):
    app = git_repo / "app.py"
    respx.post(URL).mock(return_value=llm_reply(app, GOOD))
    staged = await server.flint_write("grow", [str(app)])
    out = await server.flint_apply(str(git_repo), staged["patch_id"])
    assert out == {"applied": True, "files": ["app.py"]}
    assert app.read_text() == GOOD
    last = log_rows(git_repo)[-1]
    assert (last["tool"], last["outcome"], last["task_key"]) == (
        "flint_apply", "applied", task_key("grow"),
    )  # fmt: skip


@respx.mock
async def test_apply_tool_refusals_are_logged_as_errors(tool_env, git_repo):
    out = await server.flint_apply(str(git_repo), "abc123")
    assert out == {"error": "unknown_patch"}
    assert log_rows(git_repo)[-1]["outcome"] == "error"
    assert (await server.flint_apply("relative", "abc123"))["error"] == "relative_path"


async def test_tool_descriptions_state_the_contract():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert "read the patch file yourself" in tools["flint_apply"].description
    assert "Refuses if checks failed or the patch was modified" in tools["flint_apply"].description
    assert "counts as a failed attempt" in tools["flint_reject"].description
