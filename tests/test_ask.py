import json

import httpx
import respx

from puppet_master import server
from puppet_master.config import Config

CFG = Config()
URL = f"{CFG.base_url}/v1/chat/completions"


def llm_reply(text: str) -> httpx.Response:
    body = {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 12},
    }
    return httpx.Response(200, json=body)


def log_rows(home) -> list[dict]:
    return [json.loads(x) for x in (home / ".puppet-master" / "log.jsonl").read_text().splitlines()]


@respx.mock
async def test_draft_is_always_marked_unverified():
    respx.get(f"{CFG.base_url}/health").mock(return_value=httpx.Response(200, json={}))
    route = respx.post(URL).mock(return_value=llm_reply("Fix the login redirect"))
    out = await server.puppet_ask("Write a commit message", context="diff: fixed redirect")
    assert out == {"draft": "Fix the login redirect", "verified": False}
    user = json.loads(route.calls[0].request.content)["messages"][1]["content"]
    assert user.index("diff: fixed redirect") < user.index("Write a commit message")


@respx.mock
async def test_prompt_without_context_is_sent_as_is():
    respx.get(f"{CFG.base_url}/health").mock(return_value=httpx.Response(200, json={}))
    route = respx.post(URL).mock(return_value=llm_reply("ok"))
    await server.puppet_ask("Say hi")
    assert json.loads(route.calls[0].request.content)["messages"][1]["content"] == "Say hi"


@respx.mock
async def test_too_large_a_prompt_is_over_budget_without_calling_flint():
    health = respx.get(f"{CFG.base_url}/health").mock(return_value=httpx.Response(200))
    llm = respx.post(URL).mock(return_value=llm_reply("never"))
    out = await server.puppet_ask("x" * 64_000)
    assert out["error"] == "over_budget"
    assert not llm.called
    assert not health.called


@respx.mock
async def test_offline_flint_is_reported_as_data():
    respx.get(f"{CFG.base_url}/health").mock(return_value=httpx.Response(200, json={}))
    respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
    assert await server.puppet_ask("hi") == {"error": "flint_offline"}


@respx.mock
async def test_call_is_logged_to_home_with_token_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    respx.get(f"{CFG.base_url}/health").mock(return_value=httpx.Response(200, json={}))
    respx.post(URL).mock(return_value=llm_reply("draft"))
    await server.puppet_ask("hi")
    [row] = log_rows(tmp_path)
    assert (row["tool"], row["outcome"]) == ("puppet_ask", "ok")
    assert (row["flint_tokens_in"], row["flint_tokens_out"]) == (40, 12)


async def test_description_sends_code_questions_to_flint_read():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    description = tools["puppet_ask"].description
    assert "puppet_read" in description
    assert "UNVERIFIED" in description
