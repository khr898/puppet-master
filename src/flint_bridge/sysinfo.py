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


def available_gb() -> float | None:
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_vm_stat(out.stdout)
