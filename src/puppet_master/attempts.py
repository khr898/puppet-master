import json
import os
from pathlib import Path


def _path(root: Path) -> Path:
    return root / ".puppet-master" / "attempts.json"


def _load(root: Path) -> dict:
    try:
        data = json.loads(_path(root).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(root: Path, data: dict) -> None:
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def record(root: Path, key: str, passed: bool) -> None:
    """Count one attempt at task `key`. Only attempts that did not pass are counted."""
    if passed:
        return
    data = _load(root)
    entry = data.get(key)
    failed = entry.get("failed", 0) if isinstance(entry, dict) else 0
    data[key] = {"failed": failed + 1}
    _save(root, data)


def give_up(root: Path, key: str) -> bool:
    entry = _load(root).get(key)
    return isinstance(entry, dict) and entry.get("failed", 0) >= 2


def reset(root: Path, key: str) -> None:
    data = _load(root)
    if data.pop(key, None) is not None:
        _save(root, data)
