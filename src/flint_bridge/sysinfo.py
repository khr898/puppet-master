import re
import subprocess


def mem_pressure() -> int | None:
    """Percent of memory in use per `memory_pressure -Q` (100 - free%), or None if unavailable."""
    try:
        out = subprocess.run(
            ["memory_pressure", "-Q"], capture_output=True, text=True, timeout=5, check=True
        ).stdout
        return 100 - int(re.search(r"free percentage:\s*(\d+)%", out).group(1))
    except (OSError, subprocess.SubprocessError, AttributeError, ValueError):
        return None
