import json
from datetime import UTC, datetime
from pathlib import Path

from flint_bridge.config import Config

MAX_BYTES = 200 * 1024 * 1024


def record(
    root: Path,
    cfg: Config,
    *,
    tool: str,
    verdict: str,
    system: str | None = None,
    user: str | None = None,
    raw_output: str | None = None,
    patch_id: str | None = None,
) -> None:
    """Append one training sample to root/.flint/samples/YYYY-MM-DD.jsonl, if samples are on.

    flint_apply and flint_reject add a line with only a patch_id and the final verdict. When
    the folder passes MAX_BYTES the oldest day files go first, but never the one just written.
    """
    if not cfg.samples:
        return
    folder = root / ".flint" / "samples"
    folder.mkdir(parents=True, exist_ok=True)
    row = {
        "tool": tool,
        "system": system,
        "user": user,
        "raw_output": raw_output,
        "verdict": verdict,
        "patch_id": patch_id,
    }
    today = folder / f"{datetime.now(UTC):%Y-%m-%d}.jsonl"
    with today.open("a") as f:
        f.write(json.dumps({k: v for k, v in row.items() if v is not None}) + "\n")
    files = sorted(folder.glob("*.jsonl"))
    total = sum(p.stat().st_size for p in files)
    for old in files[:-1]:
        if total <= MAX_BYTES:
            break
        total -= old.stat().st_size
        old.unlink()
