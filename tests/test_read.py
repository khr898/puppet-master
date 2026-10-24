import json
import sys
from pathlib import Path

import httpx
import pytest
import respx

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import hallucinations as hx  # noqa: E402

from flint_bridge import server  # noqa: E402
from flint_bridge.config import Config  # noqa: E402
from flint_bridge.read import Usage, flint_read_impl  # noqa: E402
from flint_bridge.report import est_tokens  # noqa: E402

CFG = Config()
URL = f"{CFG.base_url}/v1/chat/completions"


def llm_reply(content: str | dict) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content)
    body = {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 50},
    }
    return httpx.Response(200, json=body)


def answer(claims: list, text: str = "the answer") -> dict:
    return {"answer": text, "claims": claims}


def sent_user_message(route, call: int = 0) -> str:
    return json.loads(route.calls[call].request.content)["messages"][1]["content"]


@pytest.fixture
def shapes(tmp_path) -> Path:
    path = tmp_path / "shapes.py"
    path.write_text(hx.SOURCE)
    return path


def retarget(raw: dict, path: Path) -> dict:
    """Point a fixture claim at the real temp file (keeps the 'wrong file' claim wrong)."""
    return {**raw, "file": str(path)} if raw.get("file") == hx.PATH else raw


@respx.mock
async def test_hallucinated_claims_are_stripped_and_good_ones_kept(shapes):
    claims = [retarget(c, shapes) for _, c in hx.BAD + hx.GOOD]
    respx.post(URL).mock(return_value=llm_reply(answer(claims)))
    out = await flint_read_impl(CFG, [str(shapes)], "what does Circle do?")
    assert len(out["claims"]) == len(hx.GOOD)
    assert out["stripped"] == len(hx.BAD)
    assert out["answer_confidence"] == "degraded"
    assert out["chunks"] == 1
    assert all(set(c) == {"text", "file", "line"} for c in out["claims"])


@respx.mock
async def test_clean_answer_is_not_degraded(shapes):
    good = retarget(hx.GOOD[0][1], shapes)
    respx.post(URL).mock(return_value=llm_reply(answer([good], "Circle.area uses pi")))
    out = await flint_read_impl(CFG, [str(shapes)], "q")
    assert out == {
        "answer": "Circle.area uses pi",
        "claims": [{"text": "t", "file": str(shapes), "line": 10}],
        "stripped": 0,
        "chunks": 1,
    }


@respx.mock
async def test_invalid_json_twice_is_unverifiable(shapes):
    route = respx.post(URL).mock(return_value=llm_reply("not json"))
    out = await flint_read_impl(CFG, [str(shapes)], "q")
    assert out == {"error": "unverifiable_output"}
    assert route.call_count == 2


@respx.mock
async def test_json_without_an_answer_counts_as_invalid(shapes):
    route = respx.post(URL).mock(return_value=llm_reply({"claims": []}))
    out = await flint_read_impl(CFG, [str(shapes)], "q")
    assert out == {"error": "unverifiable_output"}
    assert route.call_count == 2


@respx.mock
async def test_invalid_json_once_then_valid_succeeds_and_asks_for_json_only(shapes):
    route = respx.post(URL).mock(side_effect=[llm_reply("oops"), llm_reply(answer([], "ok"))])
    usage = Usage()
    out = await flint_read_impl(CFG, [str(shapes)], "q", usage=usage)
    assert out["answer"] == "ok"
    assert sent_user_message(route, 1).endswith(
        "Your last reply was not valid JSON. Reply with JSON only."
    )
    assert usage.retries == 1
    assert usage.tokens_in == 2000 and usage.tokens_out == 100


@respx.mock
async def test_line_range_sends_only_those_lines_with_original_numbers(shapes):
    route = respx.post(URL).mock(
        return_value=llm_reply(
            answer([{"text": "t", "file": str(shapes), "line": 15, "quote": "class Rect"}])
        )
    )
    # lines 10-20 of the fixture; line 15 is blank, the Rect class is on line 16
    out = await flint_read_impl(CFG, [f"{shapes}:10-20"], "q")
    sent = sent_user_message(route)
    assert "   10|         return math.pi * self.radius ** 2" in sent
    assert "   20|" in sent
    assert "    9|" not in sent and "   21|" not in sent
    assert out["stripped"] == 0 and len(out["claims"]) == 1  # 'class Rect' is within ±2 of 15


@respx.mock
async def test_prompt_puts_files_first_in_sorted_order_and_question_last(tmp_path):
    (tmp_path / "b.py").write_text("B\n")
    (tmp_path / "a.py").write_text("A\n")
    route = respx.post(URL).mock(return_value=llm_reply(answer([])))
    await flint_read_impl(CFG, [str(tmp_path / "b.py"), str(tmp_path / "a.py")], "why?")
    sent = sent_user_message(route)
    assert sent == (
        f"=== FILE {tmp_path / 'a.py'} ===\n    1| A\n"
        f"=== FILE {tmp_path / 'b.py'} ===\n    1| B\n"
        "\nQUESTION: why?"
    )


@respx.mock
async def test_request_uses_the_read_schema_and_1500_max_tokens(shapes):
    route = respx.post(URL).mock(return_value=llm_reply(answer([])))
    await flint_read_impl(CFG, [str(shapes)], "q")
    sent = json.loads(route.calls[0].request.content)
    assert sent["max_tokens"] == 1500
    schema = sent["response_format"]["json_schema"]["schema"]
    assert schema["required"] == ["answer", "claims"]
    assert schema["properties"]["claims"]["items"]["required"] == ["text", "file", "line", "quote"]


@respx.mock
async def test_directory_relative_and_unreadable_paths_never_call_flint(tmp_path):
    route = respx.post(URL).mock(return_value=llm_reply(answer([])))
    assert (await flint_read_impl(CFG, [str(tmp_path)], "q"))["error"] == "directory_not_allowed"
    assert (await flint_read_impl(CFG, ["src/x.py"], "q"))["error"] == "relative_path"
    missing = str(tmp_path / "nope.py")
    assert await flint_read_impl(CFG, [missing], "q") == {"error": "unreadable", "path": missing}
    binary = tmp_path / "bin.dat"
    binary.write_bytes(b"\xff\xfe\x00")
    assert (await flint_read_impl(CFG, [str(binary)], "q"))["error"] == "unreadable"
    assert route.call_count == 0


@respx.mock
async def test_bad_line_range_is_reported(shapes):
    route = respx.post(URL).mock(return_value=llm_reply(answer([])))
    for rng in ("0-5", "9-3", "500-600"):
        out = await flint_read_impl(CFG, [f"{shapes}:{rng}"], "q")
        assert out["error"] == "bad_range"
    assert route.call_count == 0


@respx.mock
async def test_over_budget_makes_no_llm_call(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("x" * 40 * 1000 + "\n" + "y" * 4000)  # > 10k tokens numbered
    route = respx.post(URL).mock(return_value=llm_reply(answer([])))
    out = await flint_read_impl(CFG, [str(big)], "q")
    assert out["error"] == "over_budget"
    assert out["limit"] == CFG.chunk_tokens
    assert out["est_tokens"] > CFG.chunk_tokens
    assert "path:start-end" in out["hint"]
    assert route.call_count == 0


@respx.mock
async def test_ready_hook_runs_after_preflight_and_can_veto(shapes, tmp_path):
    calls = []

    async def ready():
        calls.append(1)
        return {"error": "insufficient_memory"}

    route = respx.post(URL).mock(return_value=llm_reply(answer([])))
    assert await flint_read_impl(CFG, [str(tmp_path)], "q", ready=ready) == {
        "error": "directory_not_allowed",
        "path": str(tmp_path),
    }
    assert calls == []  # a bad request never starts Flint
    out = await flint_read_impl(CFG, [str(shapes)], "q", ready=ready)
    assert out == {"error": "insufficient_memory"}
    assert calls == [1] and route.call_count == 0


# --- the MCP tool -------------------------------------------------------------------------


@pytest.fixture
def tool_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(server, "mem_pressure", lambda: 10)


def mock_up():
    respx.get(f"{CFG.base_url}/health").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{CFG.base_url}/props").mock(
        return_value=httpx.Response(200, json={"default_generation_settings": {"n_ctx": 16384}})
    )


def log_rows(root: Path) -> list[dict]:
    return [json.loads(x) for x in (root / ".flint" / "log.jsonl").read_text().splitlines()]


@respx.mock
async def test_tool_report_fits_500_tokens_even_with_a_huge_answer(tool_env, shapes):
    mock_up()
    claims = [
        {"text": "word " * 20, "file": str(shapes), "line": 10, "quote": "math.pi"}
        for _ in range(200)
    ]
    respx.post(URL).mock(return_value=llm_reply(answer(claims, "long " * 3000)))
    out = await server.flint_read([str(shapes)], "q")
    assert est_tokens(json.dumps(out)) <= 500
    assert out["truncated"] is True


@respx.mock
async def test_tool_logs_usage_to_the_project_root(tool_env, shapes):
    mock_up()
    bad = retarget(hx.BAD[0][1], shapes)
    respx.post(URL).mock(return_value=llm_reply(answer([bad])))
    out = await server.flint_read([str(shapes)], "q")
    assert out["stripped"] == 1
    [row] = log_rows(shapes.parent)  # not in a git repo, so the file's parent is the root
    assert row["tool"] == "flint_read"
    assert row["outcome"] == "ok"
    assert row["stripped"] == 1
    assert row["flint_tokens_in"] == 1000 and row["flint_tokens_out"] == 50
    assert row["report_tokens"] > 0


@respx.mock
async def test_tool_returns_flint_offline(tool_env, shapes):
    mock_up()
    respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
    out = await server.flint_read([str(shapes)], "q")
    assert out == {"error": "flint_offline"}
    assert log_rows(shapes.parent)[0]["outcome"] == "offline"


@respx.mock
async def test_tool_over_budget_is_logged_and_does_not_start_flint(tool_env, tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("x" * 60000)
    health = respx.get(f"{CFG.base_url}/health").mock(return_value=httpx.Response(200))
    out = await server.flint_read([str(big)], "q")
    assert out["error"] == "over_budget"
    assert health.call_count == 0
    assert log_rows(tmp_path)[0]["outcome"] == "over_budget"


async def test_tool_description_states_the_contract():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    desc = tools["flint_read"].description
    assert "USE THIS FIRST" in desc and "path:start-end" in desc and "≤8 chunks" in desc


@respx.mock
async def test_an_oversized_question_is_over_budget_without_a_call(shapes):
    route = respx.post(URL).mock(return_value=llm_reply(answer([])))
    out = await flint_read_impl(CFG, [str(shapes)], "why " * 16000)
    assert out["error"] == "over_budget"
    assert out["limit"] == CFG.ctx - 512 - 1500
    assert route.call_count == 0
