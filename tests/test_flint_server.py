import json
import os
import socket
from pathlib import Path

import pytest

from flint_bridge import lifecycle, server
from flint_bridge.lifecycle import ServerConfig

FAKE = Path(__file__).parent / "fixtures" / "fake_server.py"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(lifecycle, "POLL_S", 0.1)
    monkeypatch.setattr(lifecycle, "free_gb", lambda: 100.0)
    monkeypatch.setattr(server, "mem_pressure", lambda: 40)
    return home


@pytest.fixture
def running_config(env, tmp_path, monkeypatch) -> ServerConfig:
    """A server.toml pointing at the fake server, and FLINT_BASE_URL to match."""
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", 0))
        except PermissionError:
            pytest.skip("local port binding is blocked (sandbox)")
        port = s.getsockname()[1]
    model = tmp_path / "flint-test.gguf"
    model.write_bytes(b"x")
    (env / ".flint").mkdir()
    (env / ".flint" / "server.toml").write_text(
        f'binary = "{FAKE}"\nmodel = "{model}"\nport = {port}\nstart_timeout_s = 10\n'
    )
    monkeypatch.setenv("FLINT_BASE_URL", f"http://127.0.0.1:{port}")
    yield lifecycle.load_server_config()
    pidfile = env / ".flint" / "server.pid"
    if pidfile.exists():
        try:
            os.kill(json.loads(pidfile.read_text())["pid"], 9)
        except ProcessLookupError:
            pass


async def test_flint_server_tool_is_registered_with_directive_description():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    desc = tools["flint_server"].description
    assert "Use stop when you have finished the delegated work" in desc
    assert "compiling" in desc
    assert "start" in json.dumps(tools["flint_server"].inputSchema)


async def test_flint_server_without_config_returns_no_server_config():
    out = await server.flint_server("start")
    assert out["error"] == "no_server_config"
    assert "flint-server.md" in out["hint"]


async def test_flint_server_with_invalid_config_returns_bad_server_config(env):
    (env / ".flint").mkdir()
    (env / ".flint" / "server.toml").write_text('binary = "/nope"\nmodel = "/nope"\n')
    assert (await server.flint_server("start"))["error"] == "bad_server_config"


async def test_flint_server_start_then_stop(running_config):
    started = await server.flint_server("start")
    assert started["up"] is True
    assert started["managed"] is True
    assert isinstance(started["pid"], int)
    assert started["rss_gb"] is None or started["rss_gb"] >= 0
    assert started["mem_free_pct"] == 60

    stopped = await server.flint_server("stop")
    assert stopped["up"] is False
    assert stopped["managed"] is False
    assert stopped["pid"] is None
    assert stopped["stopped"] is True


async def test_stop_reports_the_tokens_saved_this_session(running_config, monkeypatch):
    monkeypatch.setattr(server, "session", server.Session())
    server.session.add(cost=100, avoided=1500)
    server.session.add(cost=50, avoided=0)
    stopped = await server.flint_server("stop")
    assert stopped["session"] == {"calls": 2, "saved_tokens": 1350}


async def test_flint_server_restart_gives_a_new_pid(running_config):
    first = await server.flint_server("start")
    second = await server.flint_server("restart")
    assert second["up"] is True and second["managed"] is True
    assert second["pid"] != first["pid"]


async def test_flint_server_start_reports_insufficient_memory(running_config, monkeypatch):
    monkeypatch.setattr(lifecycle, "free_gb", lambda: 0.1)
    assert (await server.flint_server("start"))["error"] == "insufficient_memory"


async def test_status_reports_lifecycle_fields(running_config):
    before = await server.flint_status()
    assert (before["up"], before["managed"], before["pid"], before["autostart"]) == (
        False,
        False,
        None,
        True,
    )
    started = await server.flint_server("start")
    after = await server.flint_status()
    assert (after["up"], after["managed"], after["pid"]) == (True, True, started["pid"])
    await server.flint_server("stop")


async def test_status_without_server_config_has_null_lifecycle_fields():
    out = await server.flint_status()
    assert (out["managed"], out["pid"], out["autostart"]) == (False, None, None)


# ---- ensure_up ----------------------------------------------------------------------------


@pytest.fixture
def fakes(monkeypatch):
    state = {"up": False, "start_result": {"up": True, "managed": True}, "starts": 0}

    async def health(cfg):
        return {"up": state["up"]}

    async def start(sc, cfg):
        state["starts"] += 1
        return state["start_result"]

    monkeypatch.setattr(server.llm, "health", health)
    monkeypatch.setattr(lifecycle, "start", start)
    return state


def install_config(env, tmp_path, autostart: bool = True):
    model = tmp_path / "m.gguf"
    model.write_bytes(b"x")
    (env / ".flint").mkdir(exist_ok=True)
    (env / ".flint" / "server.toml").write_text(
        f'binary = "{FAKE}"\nmodel = "{model}"\nautostart = {str(autostart).lower()}\n'
    )


async def test_ensure_up_does_nothing_when_flint_is_up(fakes):
    fakes["up"] = True
    assert await server.ensure_up(server.load_config(Path.cwd())) is None
    assert fakes["starts"] == 0


async def test_ensure_up_autostarts_when_down(fakes, env, tmp_path):
    install_config(env, tmp_path)
    assert await server.ensure_up(server.load_config(Path.cwd())) is None
    assert fakes["starts"] == 1


async def test_ensure_up_returns_the_start_error(fakes, env, tmp_path):
    install_config(env, tmp_path)
    fakes["start_result"] = {"error": "insufficient_memory", "need_gb": 14.5, "free_gb": 3.0}
    out = await server.ensure_up(server.load_config(Path.cwd()))
    assert out["error"] == "insufficient_memory"


async def test_ensure_up_respects_autostart_false(fakes, env, tmp_path):
    install_config(env, tmp_path, autostart=False)
    assert await server.ensure_up(server.load_config(Path.cwd())) is None
    assert fakes["starts"] == 0


async def test_ensure_up_without_config_returns_no_server_config(fakes):
    out = await server.ensure_up(server.load_config(Path.cwd()))
    assert out["error"] == "no_server_config"


async def test_ensure_up_touches_the_idle_watcher(fakes, monkeypatch):
    touched = []
    monkeypatch.setattr(server.idle, "touch", lambda: touched.append(1))
    fakes["up"] = True
    await server.ensure_up(server.load_config(Path.cwd()))
    assert touched
