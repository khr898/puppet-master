import asyncio
import fcntl
import json
import os
import signal
import subprocess
import time
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from flint_bridge import llm, sysinfo
from flint_bridge.config import Config

CACHE_TYPES = ("f16", "q8_0", "q4_0")
_KV_GB_16K = {"f16": 1.0, "q8_0": 0.5, "q4_0": 0.25}  # per K or V tensor at 16K context
POLL_S = 1.0
STOP_WAIT_S = 10.0
_NOT_MANAGED = {"stopped": False, "reason": "not_managed"}


@dataclass(frozen=True)
class ServerConfig:
    binary: Path
    model: Path
    host: str = "127.0.0.1"
    port: int = 8000
    ctx: int = 16384
    parallel: int = 1
    gpu_layers: int = 99
    flash_attn: str = "on"
    cache_type_k: str = "q8_0"
    cache_type_v: str = "q8_0"
    cache_reuse: int = 256
    reasoning_budget: int = 0
    slot_save_path: Path = Path("~/.flint/slots")
    autostart: bool = True
    idle_stop_min: int = 15
    min_free_gb: float = 1.5
    start_timeout_s: int = 180


def _flint_dir() -> Path:
    return Path.home() / ".flint"


def load_server_config(path: Path = Path("~/.flint/server.toml")) -> ServerConfig | None:
    """Read ~/.flint/server.toml. None if missing; ValueError if it breaks a safety rule."""
    path = path.expanduser()
    if not path.exists():
        return None
    data = tomllib.loads(path.read_text())
    unknown = set(data) - {f.name for f in fields(ServerConfig)}
    if unknown:
        raise ValueError(f"unknown server.toml keys: {sorted(unknown)}")
    for key in ("binary", "model"):
        if key not in data:
            raise ValueError(f"server.toml needs {key!r}")
    for key in ("binary", "model", "slot_save_path"):
        if key in data:
            data[key] = Path(data[key]).expanduser()
    sc = ServerConfig(**data)
    if sc.host != "127.0.0.1":
        raise ValueError("host must be 127.0.0.1")
    for key in ("binary", "model"):
        if not getattr(sc, key).exists():
            raise ValueError(f"{key} does not exist: {getattr(sc, key)}")
    for key in ("cache_type_k", "cache_type_v"):
        if getattr(sc, key) not in CACHE_TYPES:
            raise ValueError(f"cache_type must be one of {CACHE_TYPES}, got {getattr(sc, key)!r}")
    if sc.slot_save_path == ServerConfig.slot_save_path:
        sc = ServerConfig(**{**data, "slot_save_path": sc.slot_save_path.expanduser()})
    return sc


def build_argv(sc: ServerConfig) -> list[str]:
    """The only way llama-server is ever launched. No tool can add flags or paths."""
    return [
        str(sc.binary), "-m", str(sc.model), "--alias", "flint",
        "--host", sc.host, "--port", str(sc.port), "-c", str(sc.ctx),
        "-np", str(sc.parallel), "-ngl", str(sc.gpu_layers), "-fa", sc.flash_attn,
        "--cache-type-k", sc.cache_type_k, "--cache-type-v", sc.cache_type_v,
        "--cache-reuse", str(sc.cache_reuse), "--reasoning-budget", str(sc.reasoning_budget),
        "--slot-save-path", str(sc.slot_save_path), "--jinja",
    ]  # fmt: skip


def free_gb() -> float | None:
    """Memory the OS can hand out without swapping, in GB. None if unavailable."""
    return sysinfo.available_gb()


def preflight(sc: ServerConfig) -> dict | None:
    """None if there's room to launch, else an insufficient_memory error dict."""
    kv_gb = (_KV_GB_16K[sc.cache_type_k] + _KV_GB_16K[sc.cache_type_v]) * sc.ctx / 16384
    need = sc.model.stat().st_size / 1e9 + kv_gb + sc.min_free_gb
    free = free_gb()
    if free is None or free >= need:
        return None
    return {
        "error": "insufficient_memory",
        "need_gb": round(need, 1),
        "free_gb": round(free, 1),
        "hint": "close apps or lower ctx in ~/.flint/server.toml",
    }


# ---- process bookkeeping -----------------------------------------------------------------


def _stamp() -> Path:
    return _flint_dir() / "last_used"


def _pidfile() -> Path:
    return _flint_dir() / "server.pid"


def _ps(field: str, pid: int) -> str:
    try:
        out = subprocess.run(
            ["ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def _alive(pid: int) -> bool:
    stat = _ps("stat", pid)
    return bool(stat) and not stat.startswith("Z")  # a zombie is already dead


def _read_pid() -> int | None:
    try:
        return int(json.loads(_pidfile().read_text())["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def managed_pid(sc: ServerConfig) -> int | None:
    """pid of the llama-server this bridge family started, verified by its command line."""
    pid = _read_pid()
    if pid is None or not _alive(pid):
        return None
    cmd = _ps("command", pid)
    return pid if sc.binary.name in cmd and sc.model.name in cmd else None


def managed(sc: ServerConfig) -> bool:
    return managed_pid(sc) is not None


def rss_gb(pid: int) -> float | None:
    try:
        return round(int(_ps("rss", pid)) / 1024 / 1024, 2)
    except ValueError:
        return None


def _log_tail(n: int = 10) -> list[str]:
    try:
        return (_flint_dir() / "server.log").read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


# ---- start / stop ------------------------------------------------------------------------


async def start(sc: ServerConfig, cfg: Config) -> dict:
    _flint_dir().mkdir(parents=True, exist_ok=True)
    lock = (_flint_dir() / "server.lock").open("a+")
    try:
        while True:  # exclusive lock without blocking the event loop
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.1)
        if (await llm.health(cfg))["up"]:
            return {"up": True, "managed": managed(sc)}
        if (err := preflight(sc)) is not None:
            return err
        return await _launch(sc, cfg)
    finally:
        lock.close()  # releases the flock


async def _launch(sc: ServerConfig, cfg: Config) -> dict:
    sc.slot_save_path.mkdir(parents=True, exist_ok=True)
    with (_flint_dir() / "server.log").open("ab") as log:
        proc = subprocess.Popen(
            build_argv(sc), stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
    _pidfile().write_text(
        json.dumps({"pid": proc.pid, "argv0": str(sc.binary), "started": time.time()})
    )
    deadline = time.monotonic() + sc.start_timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _pidfile().unlink(missing_ok=True)
            return {"error": "start_failed", "exit_code": proc.returncode, "log_tail": _log_tail()}
        if (await llm.health(cfg))["up"]:
            return {"up": True, "managed": True, "pid": proc.pid}
        await asyncio.sleep(POLL_S)
    proc.kill()
    proc.wait()
    _pidfile().unlink(missing_ok=True)
    return {"error": "start_timeout", "log_tail": _log_tail()}


def _wait_gone(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        try:
            os.waitpid(pid, os.WNOHANG)  # reap it if it's our child
        except ChildProcessError:
            pass
        if not _alive(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def stop(sc: ServerConfig) -> dict:
    """SIGTERM (then SIGKILL) the server in the pidfile, only if its command line matches."""
    pid = _read_pid()
    if pid is None:
        return dict(_NOT_MANAGED)
    if not _alive(pid):
        _pidfile().unlink(missing_ok=True)
        return dict(_NOT_MANAGED)
    if managed_pid(sc) != pid:
        return dict(_NOT_MANAGED)
    try:
        os.kill(pid, signal.SIGTERM)
        if not _wait_gone(pid, STOP_WAIT_S):
            os.kill(pid, signal.SIGKILL)
            _wait_gone(pid, 2)
    except ProcessLookupError:
        pass
    _pidfile().unlink(missing_ok=True)
    return {"stopped": True}


class IdleWatcher:
    """Stops a managed server after sc.idle_stop_min with no tool calls from any bridge.

    Every Claude session runs its own bridge, and they share one server. Each use is stamped
    in ~/.flint/last_used, so a session that has gone quiet never stops a server another
    session (or flint-eval) is still using.
    """

    def __init__(self, clock=time.time):
        self._clock = clock
        self._last = clock()
        self._stopped_at: float | None = None

    def touch(self) -> None:
        self._last = self._clock()
        self._stopped_at = None
        try:
            _stamp().parent.mkdir(parents=True, exist_ok=True)
            _stamp().write_text(repr(self._last))
        except OSError:
            pass  # without the stamp, only this process's own use counts

    def _shared_last(self) -> float:
        try:
            return float(_stamp().read_text())
        except (OSError, ValueError):
            return 0.0

    def check(self, sc: ServerConfig) -> bool:
        shared = self._shared_last()
        if self._stopped_at is not None and shared <= self._stopped_at:
            return False  # already stopped, and nobody has used it since
        if self._clock() - max(self._last, shared) <= sc.idle_stop_min * 60:
            return False
        if not managed(sc):
            return False
        stop(sc)
        self._stopped_at = self._clock()
        return True

    async def run(self, sc: ServerConfig, interval: float = 60.0) -> None:
        while True:
            await asyncio.sleep(interval)
            await asyncio.to_thread(self.check, sc)
