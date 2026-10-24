import re
import subprocess
from pathlib import Path

from flint_bridge.config import Config

MAX_FAILURES = 20
MAX_FAILURE_CHARS = 200
_FAILURE = re.compile(r"^(FAILED|ERROR) ")
_LINT = re.compile(r"^\S+:\d+:\d+: ")


def extract_failures(output: str, exit_code: int = 0) -> list[str]:
    """Failure lines from check output: pytest FAILED/ERROR lines and ruff/mypy diagnostics.

    With none of those on a failing run, the last 20 lines stand in for them.
    """
    lines = output.splitlines()
    found = [ln for ln in lines if _FAILURE.match(ln) or _LINT.match(ln)]
    if not found and exit_code != 0:
        found = [ln for ln in lines if ln.strip()][-MAX_FAILURES:]
    return [ln[:MAX_FAILURE_CHARS] for ln in found[:MAX_FAILURES]]


def run_check(cfg: Config, cwd: Path, name: str) -> dict:
    """Run the allow-listed check `name` in cwd. `name` is looked up, never parsed or executed."""
    argv = cfg.check_commands.get(name)
    if argv is None:
        return {"error": "not_allowed", "allowed": sorted(cfg.check_commands)}
    try:
        run = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=cfg.check_timeout_s
        )
    except subprocess.TimeoutExpired:
        return {"passed": False, "exit_code": None, "failures": ["timeout"]}
    except OSError:
        return {"passed": False, "exit_code": None, "failures": ["command_not_found"]}
    return {
        "passed": run.returncode == 0,
        "exit_code": run.returncode,
        "failures": extract_failures(run.stdout + run.stderr, run.returncode),
    }
