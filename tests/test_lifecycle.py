import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from puppet_master import lifecycle
from puppet_master.config import Config
from puppet_master.lifecycle import (
    IdleWatcher,
    ServerConfig,
    build_argv,
    load_server_config,
    preflight,
    start,
    stop,
)

FAKE = Path(__file__).parent / "fixtures" / "fake_server.py"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(lifecycle, "POLL_S", 0.1)
    monkeypatch.setattr(lifecycle, "free_gb", lambda: 100.0)
    return h


@pytest.fixture
def port() -> int:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", 0))
        except PermissionError:
            pytest.skip("local port binding is blocked (sandbox)")
        return s.getsockname()[1]


@pytest.fixture
def model(tmp_path) -> Path:
    m = tmp_path / "flint-test.gguf"
    m.write_bytes(b"x")
    return m


@pytest.fixture
def sc(port, model) -> ServerConfig:
    return ServerConfig(binary=FAKE, model=model, port=port, start_timeout_s=10)


@pytest.fixture
def cfg(port) -> Config:
    return Config(base_url=f"http://127.0.0.1:{port}")


@pytest.fixture(autouse=True)
def reap_servers(home):
    yield
    pidfile = home / ".puppet-master" / "server.pid"
    if pidfile.exists():
        try:
            pid = json.loads(pidfile.read_text())["pid"]
            if pid != os.getpid():  # one test points the pidfile at the runner itself
                os.kill(pid, 9)
        except (ProcessLookupError, ValueError, KeyError):
            pass


def write_toml(home: Path, body: str) -> Path:
    path = home / ".puppet-master" / "server.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def pid_alive(pid: int) -> bool:
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(stat.stdout.strip()) and not stat.stdout.strip().startswith("Z")


# ---- config -------------------------------------------------------------------------------


def test_load_server_config_returns_none_when_file_missing(home):
    assert load_server_config() is None


def test_load_server_config_expands_tilde_and_applies_defaults(home, model):
    (home / "bin").mkdir()
    (home / "bin" / "llama-server").write_text("")
    write_toml(home, f'binary = "~/bin/llama-server"\nmodel = "{model}"\n')
    sc = load_server_config()
    assert sc.binary == home / "bin" / "llama-server"
    assert sc.slot_save_path == home / ".puppet-master" / "slots"
    assert (sc.host, sc.port, sc.ctx, sc.parallel) == ("127.0.0.1", 8000, 16384, 1)
    assert (sc.cache_type_k, sc.cache_reuse, sc.reasoning_budget) == ("q8_0", 256, 0)
    assert (sc.autostart, sc.idle_stop_min, sc.min_free_gb, sc.start_timeout_s) == (
        True,
        15,
        1.5,
        180,
    )


def test_non_loopback_host_is_rejected(home, model):
    write_toml(home, f'binary = "{FAKE}"\nmodel = "{model}"\nhost = "0.0.0.0"\n')
    with pytest.raises(ValueError, match="127.0.0.1"):
        load_server_config()


def test_missing_binary_or_model_is_rejected(home, model):
    write_toml(home, f'binary = "/nope/llama-server"\nmodel = "{model}"\n')
    with pytest.raises(ValueError, match="binary"):
        load_server_config()
    write_toml(home, f'binary = "{FAKE}"\nmodel = "/nope/model.gguf"\n')
    with pytest.raises(ValueError, match="model"):
        load_server_config()


def test_bad_cache_type_is_rejected(home, model):
    write_toml(home, f'binary = "{FAKE}"\nmodel = "{model}"\ncache_type_k = "q5_1"\n')
    with pytest.raises(ValueError, match="cache_type"):
        load_server_config()


def test_unknown_key_is_rejected(home, model):
    write_toml(home, f'binary = "{FAKE}"\nmodel = "{model}"\nextra_flags = "--evil"\n')
    with pytest.raises(ValueError, match="extra_flags"):
        load_server_config()


# ---- argv ---------------------------------------------------------------------------------


def test_build_argv_is_exactly_the_specified_list():
    sc = ServerConfig(binary=Path("/b/llama-server"), model=Path("/m/flint.gguf"))
    assert build_argv(sc) == [
        "/b/llama-server", "-m", "/m/flint.gguf", "--alias", "flint",
        "--host", "127.0.0.1", "--port", "8000", "-c", "16384", "-np", "1", "-ngl", "99",
        "-fa", "on", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
        "--cache-reuse", "256", "--reasoning-budget", "0",
        "--slot-save-path", str(Path("~/.puppet-master/slots")), "--jinja",
    ]  # fmt: skip


# ---- preflight ----------------------------------------------------------------------------


def test_preflight_reports_insufficient_memory(tmp_path, monkeypatch):
    big = tmp_path / "big.gguf"
    with big.open("wb") as f:
        f.truncate(12_000_000_000)
    monkeypatch.setattr(lifecycle, "free_gb", lambda: 10.0)
    out = preflight(ServerConfig(binary=FAKE, model=big))
    assert out["error"] == "insufficient_memory"
    assert out["free_gb"] == 10.0
    assert out["need_gb"] == pytest.approx(14.5, abs=0.1)  # 12 model + 1 KV (q8_0) + 1.5 headroom
    assert "hint" in out


def test_preflight_kv_estimate_doubles_for_f16(tmp_path, monkeypatch):
    m = tmp_path / "m.gguf"
    m.write_bytes(b"x")
    monkeypatch.setattr(lifecycle, "free_gb", lambda: 0.0)
    sc = ServerConfig(binary=FAKE, model=m, cache_type_k="f16", cache_type_v="f16")
    assert preflight(sc)["need_gb"] == pytest.approx(3.5, abs=0.1)


def test_preflight_passes_with_enough_memory_or_unknown_memory(sc, monkeypatch):
    assert preflight(sc) is None
    monkeypatch.setattr(lifecycle, "free_gb", lambda: None)
    assert preflight(sc) is None


async def test_start_does_not_launch_when_memory_is_insufficient(sc, cfg, monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError("must not launch llama-server")

    monkeypatch.setattr(lifecycle, "free_gb", lambda: 0.1)
    monkeypatch.setattr(lifecycle, "_launch", forbidden)
    out = await start(sc, cfg)
    assert out["error"] == "insufficient_memory"


# ---- start / stop -------------------------------------------------------------------------


async def test_start_launches_server_writes_pidfile_and_stop_terminates_it(sc, cfg, home):
    out = await start(sc, cfg)
    assert out["up"] is True
    assert out["managed"] is True
    info = json.loads((home / ".puppet-master" / "server.pid").read_text())
    assert info["pid"] == out["pid"]
    assert info["argv0"] == str(FAKE)
    assert "started" in info
    assert pid_alive(info["pid"])

    result = stop(sc)

    assert result == {"stopped": True}
    assert not pid_alive(info["pid"])
    assert not (home / ".puppet-master" / "server.pid").exists()


async def test_start_logs_server_output_to_server_log(sc, cfg, home):
    await start(sc, cfg)
    stop(sc)
    assert "fake llama-server starting" in (home / ".puppet-master" / "server.log").read_text()


async def test_start_when_already_managed_and_up_does_not_launch_again(sc, cfg, monkeypatch):
    first = await start(sc, cfg)

    def forbidden(*a, **kw):
        raise AssertionError("must not launch llama-server")

    monkeypatch.setattr(lifecycle, "_launch", forbidden)
    again = await start(sc, cfg)
    assert again == {"up": True, "managed": True}
    assert first["pid"] == lifecycle.managed_pid(sc)


async def test_start_reuses_a_server_it_did_not_start_and_never_kills_it(sc, cfg, port):
    foreign = subprocess.Popen([str(FAKE), "-m", str(sc.model), "--port", str(port)])
    try:
        for _ in range(50):
            if (await lifecycle.llm.health(cfg))["up"]:
                break
            time.sleep(0.1)
        out = await start(sc, cfg)
        assert out == {"up": True, "managed": False}
        assert stop(sc) == {"stopped": False, "reason": "not_managed"}
        assert foreign.poll() is None
    finally:
        foreign.kill()
        foreign.wait()


def test_stop_refuses_pidfile_pointing_at_unrelated_process(sc, home):
    pidfile = home / ".puppet-master" / "server.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(json.dumps({"pid": os.getpid(), "argv0": "x", "started": 0}))
    assert stop(sc) == {"stopped": False, "reason": "not_managed"}
    assert pid_alive(os.getpid())


def test_stop_without_pidfile_is_not_managed(sc):
    assert stop(sc) == {"stopped": False, "reason": "not_managed"}


def test_stop_with_stale_pidfile_removes_it(sc, home):
    pidfile = home / ".puppet-master" / "server.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(json.dumps({"pid": 999_999, "argv0": "x", "started": 0}))
    assert stop(sc) == {"stopped": False, "reason": "not_managed"}
    assert not pidfile.exists()


async def test_start_timeout_kills_process_and_returns_log_tail(sc, cfg, home, monkeypatch):
    monkeypatch.setenv("FAKE_SERVER_MODE", "never")
    sc = ServerConfig(binary=sc.binary, model=sc.model, port=sc.port, start_timeout_s=1)
    out = await start(sc, cfg)
    assert out["error"] == "start_timeout"
    assert any("fake llama-server" in line for line in out["log_tail"])
    assert not (home / ".puppet-master" / "server.pid").exists()


async def test_start_reports_early_exit_without_waiting_for_timeout(sc, cfg, home, monkeypatch):
    monkeypatch.setenv("FAKE_SERVER_MODE", "exit")
    began = time.monotonic()
    out = await start(sc, cfg)
    assert out["error"] == "start_failed"
    assert time.monotonic() - began < 5
    assert not (home / ".puppet-master" / "server.pid").exists()


# ---- idle watcher -------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def stops(monkeypatch) -> list:
    calls = []
    monkeypatch.setattr(lifecycle, "stop", lambda sc: calls.append(sc) or {"stopped": True})
    monkeypatch.setattr(lifecycle, "managed", lambda sc: True)
    return calls


def test_idle_watcher_stops_a_managed_server_once_when_idle(sc, stops):
    clock = Clock()
    watcher = IdleWatcher(clock)
    sc0 = ServerConfig(binary=sc.binary, model=sc.model, idle_stop_min=0)
    clock.now = 1.0
    assert watcher.check(sc0) is True
    clock.now = 2.0
    assert watcher.check(sc0) is False
    assert len(stops) == 1


def test_idle_watcher_stops_again_after_a_new_touch(sc, stops):
    clock = Clock()
    watcher = IdleWatcher(clock)
    sc0 = ServerConfig(binary=sc.binary, model=sc.model, idle_stop_min=0)
    clock.now = 1.0
    watcher.check(sc0)
    watcher.touch()
    clock.now = 2.0
    watcher.check(sc0)
    assert len(stops) == 2


def test_idle_watcher_leaves_recently_used_server_alone(sc, stops):
    clock = Clock()
    watcher = IdleWatcher(clock)
    clock.now = 14 * 60
    assert watcher.check(sc) is False  # idle_stop_min defaults to 15
    clock.now = 15 * 60 + 1
    assert watcher.check(sc) is True


def test_idle_watcher_never_stops_an_unmanaged_server(sc, stops, monkeypatch):
    monkeypatch.setattr(lifecycle, "managed", lambda sc: False)
    clock = Clock()
    watcher = IdleWatcher(clock)
    clock.now = 10_000
    assert watcher.check(sc) is False
    assert stops == []


def test_idle_watcher_counts_use_from_another_bridge_process(sc, stops):
    """Each Claude session runs its own bridge; an idle one must not stop a server in use."""
    clock = Clock()
    idle_session, busy_session = IdleWatcher(clock), IdleWatcher(clock)
    clock.now = 14 * 60
    busy_session.touch()
    clock.now = 20 * 60
    assert idle_session.check(sc) is False
    clock.now = 30 * 60
    assert idle_session.check(sc) is True


def test_idle_watcher_rearms_when_another_process_uses_the_server(sc, stops):
    clock = Clock()
    a, b = IdleWatcher(clock), IdleWatcher(clock)
    clock.now = 16 * 60
    assert a.check(sc) is True
    clock.now = 17 * 60
    b.touch()  # another session autostarts Flint again
    clock.now = 33 * 60
    assert a.check(sc) is True
    assert len(stops) == 2
