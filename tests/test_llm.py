import json
from dataclasses import replace

import httpx
import pytest
import respx

from puppet_master.config import Config
from puppet_master.llm import Completion, FlintOffline, complete, health

CFG = Config()
URL = f"{CFG.base_url}/v1/chat/completions"


def reply(content: str, usage: dict | None = None, **message) -> httpx.Response:
    body = {
        "choices": [{"message": {"content": content, **message}}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20},
    }
    return httpx.Response(200, json=body)


@respx.mock
async def test_complete_parses_content_and_usage():
    respx.post(URL).mock(return_value=reply("hello"))
    out = await complete(CFG, "sys", "user", max_tokens=50)
    assert out == Completion(text="hello", tokens_in=100, tokens_out=20, reasoning_tokens=0)


@respx.mock
async def test_complete_sends_messages_and_always_sends_cache_prompt():
    route = respx.post(URL).mock(return_value=reply("x"))
    await complete(CFG, "SYS", "USER", max_tokens=77)
    sent = json.loads(route.calls.last.request.content)
    assert sent["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USER"},
    ]
    assert sent["model"] == "flint"
    assert sent["temperature"] == 0
    assert sent["max_tokens"] == 77
    assert sent["cache_prompt"] is True
    assert "response_format" not in sent


@respx.mock
async def test_complete_sends_json_schema_response_format_when_schema_given():
    route = respx.post(URL).mock(return_value=reply("{}"))
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    await complete(CFG, "s", "u", max_tokens=10, schema=schema)
    sent = json.loads(route.calls.last.request.content)
    assert sent["response_format"] == {"type": "json_schema", "json_schema": {"schema": schema}}
    assert sent["cache_prompt"] is True


@respx.mock
async def test_think_block_is_stripped_and_counted_as_reasoning():
    respx.post(URL).mock(return_value=reply('<think>let me think about it</think>{"a":1}'))
    out = await complete(CFG, "s", "u", max_tokens=10)
    assert out.text == '{"a":1}'
    assert out.reasoning_tokens > 0


@respx.mock
async def test_reasoning_tokens_prefer_usage_details():
    usage = {
        "prompt_tokens": 5,
        "completion_tokens": 50,
        "completion_tokens_details": {"reasoning_tokens": 31},
    }
    respx.post(URL).mock(return_value=reply("answer", usage=usage))
    assert (await complete(CFG, "s", "u", max_tokens=10)).reasoning_tokens == 31


@respx.mock
async def test_reasoning_content_is_never_returned():
    respx.post(URL).mock(return_value=reply("answer", reasoning_content="secret thoughts"))
    out = await complete(CFG, "s", "u", max_tokens=10)
    assert out.text == "answer"
    assert "secret" not in repr(out)


@respx.mock
async def test_missing_usage_falls_back_to_estimates():
    body = {"choices": [{"message": {"content": "a" * 40}}]}
    respx.post(URL).mock(return_value=httpx.Response(200, json=body))
    out = await complete(CFG, "s" * 80, "u" * 80, max_tokens=10)
    assert out.tokens_out == 10
    assert out.tokens_in == 40


@respx.mock
async def test_timeout_raises_flint_offline():
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(FlintOffline):
        await complete(CFG, "s", "u", max_tokens=10)


@respx.mock
async def test_http_500_raises_flint_offline():
    respx.post(URL).mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(FlintOffline):
        await complete(CFG, "s", "u", max_tokens=10)


@respx.mock
async def test_connection_refused_raises_flint_offline():
    respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(FlintOffline):
        await complete(CFG, "s", "u", max_tokens=10)


@respx.mock
async def test_malformed_body_raises_flint_offline():
    respx.post(URL).mock(return_value=httpx.Response(200, json={"nope": 1}))
    with pytest.raises(FlintOffline):
        await complete(CFG, "s", "u", max_tokens=10)


@respx.mock
async def test_health_reports_up_with_ctx_from_props():
    respx.get(f"{CFG.base_url}/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.get(f"{CFG.base_url}/props").mock(
        return_value=httpx.Response(200, json={"default_generation_settings": {"n_ctx": 8192}})
    )
    out = await health(CFG)
    assert out["up"] is True
    assert out["ctx"] == 8192
    assert out["latency_ms"] >= 0


@respx.mock
async def test_health_falls_back_to_config_ctx_when_props_lack_it():
    respx.get(f"{CFG.base_url}/health").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{CFG.base_url}/props").mock(return_value=httpx.Response(200, json={}))
    assert (await health(CFG))["ctx"] == CFG.ctx


@respx.mock
async def test_health_down_when_cannot_connect():
    respx.get(f"{CFG.base_url}/health").mock(side_effect=httpx.ConnectError("refused"))
    assert await health(CFG) == {"up": False}


@respx.mock
async def test_health_down_on_503_loading():
    respx.get(f"{CFG.base_url}/health").mock(return_value=httpx.Response(503))
    assert (await health(CFG))["up"] is False


@respx.mock
async def test_complete_sends_the_configured_temperature():
    route = respx.post(URL).mock(return_value=reply("x"))
    await complete(replace(CFG, temperature=0.7), "s", "u", max_tokens=5)
    assert json.loads(route.calls.last.request.content)["temperature"] == 0.7


@respx.mock
async def test_complete_sends_the_configured_model_id():
    route = respx.post(URL).mock(return_value=reply("x"))
    await complete(replace(CFG, model="qwen3.5:9b"), "s", "u", max_tokens=5)
    assert json.loads(route.calls.last.request.content)["model"] == "qwen3.5:9b"


@respx.mock
async def test_health_falls_back_to_v1_models_on_servers_without_health():
    """Ollama, LM Studio and others answer /v1/models but not llama.cpp's /health."""
    respx.get(f"{CFG.base_url}/health").mock(return_value=httpx.Response(404))
    respx.get(f"{CFG.base_url}/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "qwen3.5:9b"}]})
    )
    respx.get(f"{CFG.base_url}/props").mock(return_value=httpx.Response(404))
    out = await health(CFG)
    assert out["up"] is True
    assert out["ctx"] == CFG.ctx


@respx.mock
async def test_health_is_down_when_neither_endpoint_answers():
    respx.get(f"{CFG.base_url}/health").mock(return_value=httpx.Response(404))
    respx.get(f"{CFG.base_url}/v1/models").mock(return_value=httpx.Response(404))
    assert (await health(CFG))["up"] is False


@respx.mock
async def test_api_key_env_is_sent_as_a_bearer_token(monkeypatch):
    monkeypatch.setenv("PUPPET_API_KEY", "test-key")
    route = respx.post(URL).mock(return_value=reply("x"))
    await complete(CFG, "s", "u", max_tokens=10)
    assert route.calls.last.request.headers["authorization"] == "Bearer test-key"


@respx.mock
async def test_no_authorization_header_without_an_api_key(monkeypatch):
    monkeypatch.delenv("PUPPET_API_KEY", raising=False)
    route = respx.post(URL).mock(return_value=reply("x"))
    await complete(CFG, "s", "u", max_tokens=10)
    assert "authorization" not in route.calls.last.request.headers
