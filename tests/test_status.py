import json

import httpx
import pytest
import respx

from puppet_master import server
from puppet_master.config import Config
from puppet_master.report import est_tokens

BASE = Config().base_url
BUDGET_KEYS = {"chunk_tokens", "max_chunks", "write_max_tokens", "write_max_files", "timeout_s"}


@pytest.fixture(autouse=True)
def fixed_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(server, "mem_pressure", lambda: 42)


@respx.mock
async def test_status_when_up():
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    respx.get(f"{BASE}/props").mock(
        return_value=httpx.Response(200, json={"default_generation_settings": {"n_ctx": 16384}})
    )
    out = await server.puppet_status()
    assert out["up"] is True
    assert out["model"] == "flint"
    assert out["ctx"] == 16384
    assert out["mem_pressure"] == 42
    assert out["budgets"] == {
        "chunk_tokens": 10000,
        "max_chunks": 8,
        "write_max_tokens": 6000,
        "write_max_files": 3,
        "timeout_s": 90,
    }


@respx.mock
async def test_status_returns_budgets_even_when_flint_is_down():
    respx.get(f"{BASE}/health").mock(side_effect=httpx.ConnectError("refused"))
    out = await server.puppet_status()
    assert out["up"] is False
    assert set(out["budgets"]) == BUDGET_KEYS
    assert out["ctx"] == 16384


@respx.mock
async def test_status_is_logged_to_home(tmp_path):
    respx.get(f"{BASE}/health").mock(side_effect=httpx.ConnectError("refused"))
    await server.puppet_status()
    log = (tmp_path / ".puppet-master" / "log.jsonl").read_text().splitlines()
    [row] = [json.loads(x) for x in log]
    assert row["tool"] == "puppet_status"
    assert row["outcome"] == "ok"
    assert row["report_tokens"] > 0


@respx.mock
async def test_status_report_is_within_cap():
    respx.get(f"{BASE}/health").mock(side_effect=httpx.ConnectError("refused"))
    assert est_tokens(json.dumps(await server.puppet_status())) <= 500


def test_instructions_are_the_exact_policy_text():
    assert server.INSTRUCTIONS.startswith("You have a local model, Flint (16K context)")
    assert server.INSTRUCTIONS.endswith("tell the user.")
    assert server.mcp.instructions == server.INSTRUCTIONS


async def test_status_tool_is_registered_with_description():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert "Call once per session" in tools["puppet_status"].description


@respx.mock
async def test_status_survives_unwritable_log(monkeypatch):
    def boom(*a, **kw):
        raise PermissionError("read-only home")

    monkeypatch.setattr(server, "log_call", boom)
    respx.get(f"{BASE}/health").mock(side_effect=httpx.ConnectError("refused"))
    assert (await server.puppet_status())["up"] is False


def test_instructions_tell_claude_to_stop_flint_before_heavy_builds():
    assert "puppet_server" in server.INSTRUCTIONS
    assert "compil" in server.INSTRUCTIONS


@respx.mock
async def test_status_reports_the_configured_model(tmp_path, monkeypatch):
    (tmp_path / "puppet-master.toml").write_text('[server]\nmodel = "mellum2.1"\n')
    monkeypatch.chdir(tmp_path)
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{BASE}/props").mock(return_value=httpx.Response(404))
    assert (await server.puppet_status())["model"] == "mellum2.1"
