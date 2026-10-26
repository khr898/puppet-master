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


def parse_vm_stat(text: str) -> float | None:
    """GB of memory macOS can hand out without swapping: free + file-backed cache + purgeable.

    Free pages alone read near zero on a healthy Mac, because macOS keeps spare RAM as cache.
    Inactive anonymous pages are deliberately left out: reclaiming them means compressing or
    swapping an app's memory.
    """
    try:
        page_size = int(re.search(r"page size of (\d+) bytes", text).group(1))
        pages = {
            name: int(re.search(rf"^{name}:\s+(\d+)\.", text, re.MULTILINE).group(1))
            for name in ("Pages free", "File-backed pages", "Pages purgeable")
        }
    except (AttributeError, ValueError):
        return None
    return sum(pages.values()) * page_size / 1e9


def parse_memory_pressure(text: str) -> float | None:
    """GB macOS itself calls free: `memory_pressure -Q` free% of total memory."""
    try:
        total = int(re.search(r"The system has (\d+)", text).group(1))
        pct = int(re.search(r"free percentage:\s*(\d+)%", text).group(1))
    except (AttributeError, ValueError):
        return None
    return total * pct / 100 / 1e9


def _run(cmd: list[str]) -> str | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def available_gb() -> float | None:
    """Memory we can load a model into, in GB. None if neither measurement works.

    Takes the more lenient of two views. memory_pressure counts app memory that macOS can
    compress as free; vm_stat counts only free pages, file cache and purgeable pages. Using the
    larger avoids refusing a start that macOS would handle, at the cost of possible
    compression or swap when apps are using a lot of memory.
    """
    readings = [
        parse_vm_stat(_run(["vm_stat"]) or ""),
        parse_memory_pressure(_run(["memory_pressure", "-Q"]) or ""),
    ]
    known = [r for r in readings if r is not None]
    return max(known) if known else None
