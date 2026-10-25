"""Each logged call carries what it cost Claude and, when it delivered, what it saved Claude."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from flint_bridge import read, server
from flint_bridge.config import Config
from flint_bridge.report import compact, est_tokens
from flint_bridge.savings import added_tokens

CFG = Config()
URL = f"{CFG.base_url}/v1/chat/completions"
SOURCE = "import math\n\n\ndef area(r):\n    return math.pi * r * r\n" + "# pad\n" * 40
GOOD = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"


def reply(content) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content)
    body = {"choices": [{"message": {"content": text}}], "usage": {}}
    return httpx.Response(200, json=body)


def rows(root: Path) -> list[dict]:
    return [json.loads(x) for x in (root / ".flint" / "log.jsonl").read_text().splitlines()]


@pytest.fixture
def up(monkeypatch):
    monkeypatch.setattr(server, "mem_pressure", lambda: 10)
    respx.get(f"{CFG.base_url}/health").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{CFG.base_url}/props").mock(
        return_value=httpx.Response(200, json={"default_generation_settings": {"n_ctx": 16384}})
    )


@pytest.fixture
def source(tmp_path) -> Path:
    path = tmp_path / "geo.py"
    path.write_text(SOURCE)
    return path


@respx.mock
async def test_a_read_costs_its_report_and_the_cited_lines_and_saves_what_flint_saw(up, source):
    claim = {"text": "uses pi", "file": str(source), "line": 5, "quote": "math.pi"}
    respx.post(URL).mock(return_value=reply({"answer": "uses pi", "claims": [claim]}))
    out = await server.flint_read([str(source)], "what does area use?")
    [row] = rows(source.parent)
    cited = est_tokens("\n".join(SOURCE.splitlines()[2:7]))  # line 5, ±2
    assert row["cost_in"] == row["report_tokens"] + cited
    assert row["report_tokens"] == est_tokens(compact(out))
    args = {"paths": [str(source)], "question": "what does area use?", "exhaustive": False}
    assert row["cost_out"] == est_tokens(compact(args))
    assert row["avoided_in"] == est_tokens(read._render(read._load_files([str(source)])))
    assert row["avoided_out"] is None


@respx.mock
async def test_a_failed_read_costs_its_report_and_saves_nothing(up, tmp_path):
    missing = tmp_path / "nope.py"
    await server.flint_read([str(missing)], "q")
    [row] = rows(tmp_path)
    assert row["outcome"] == "error"
    assert row["cost_in"] == row["report_tokens"]
    assert row["avoided_in"] is None and row["avoided_out"] is None


@respx.mock
async def test_a_write_costs_the_patch_and_is_credited_only_when_applied(up, git_repo):
    app = git_repo / "app.py"
    original = app.read_text()
    files = {"files": [{"path": str(app), "content": GOOD}], "notes": "n"}
    respx.post(URL).mock(return_value=reply(files))
    out = await server.flint_write("add sub", [str(app)])
    patch = Path(out["patch_path"]).read_text()
    await server.flint_apply(str(git_repo), out["patch_id"])

    write_row, apply_row = rows(git_repo)
    assert write_row["cost_in"] == write_row["report_tokens"] + est_tokens(patch)
    assert write_row["avoided_in"] is None
    assert apply_row["outcome"] == "applied"
    assert apply_row["avoided_in"] == est_tokens(original)
    assert apply_row["avoided_out"] == added_tokens(patch) > 0


@respx.mock
async def test_a_rejected_write_has_cost_but_no_savings(up, git_repo):
    app = git_repo / "app.py"
    files = {"files": [{"path": str(app), "content": GOOD}], "notes": "n"}
    respx.post(URL).mock(return_value=reply(files))
    out = await server.flint_write("add sub", [str(app)])
    await server.flint_reject(str(git_repo), out["patch_id"], "no")
    for row in rows(git_repo):
        assert row["cost_in"] > 0
        assert row["avoided_in"] is None and row["avoided_out"] is None


@respx.mock
async def test_an_ask_saves_claude_writing_the_draft(up, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    respx.post(URL).mock(return_value=reply("Fix the login redirect"))
    await server.flint_ask("Write a commit message")
    [row] = rows(tmp_path)
    assert row["avoided_out"] == est_tokens("Fix the login redirect")
    assert row["avoided_in"] is None
    assert row["cost_out"] == est_tokens(
        compact({"prompt": "Write a commit message", "context": ""})
    )
