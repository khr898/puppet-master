import json
import math

PROTECTED = frozenset({"error", "patch_id", "patch_path", "passed", "applied"})
TRIMMABLE_LISTS = ("claims", "failures", "files", "notes")


def est_tokens(s: str) -> int:
    return math.ceil(len(s) / 4)


def _size(d: dict) -> int:
    return len(json.dumps(d))


def cap(d: dict, max_tokens: int = 500) -> dict:
    """Trim d until json.dumps(d) fits max_tokens*4 chars. Never drops protected keys."""
    limit = max_tokens * 4
    out = dict(d)
    while _size(out) > limit:
        lists = [k for k in TRIMMABLE_LISTS if isinstance(out.get(k), list) and out[k]]
        if lists:
            key = max(lists, key=lambda k: len(out[k]))
            out[key] = out[key][:-1]
        else:
            strings = [
                k
                for k, v in out.items()
                if isinstance(v, str) and len(v) > 1 and k not in PROTECTED
            ]
            if not strings:
                break
            key = max(strings, key=lambda k: len(out[k]))
            out[key] = out[key][: len(out[key]) // 2] + "…"
        out["truncated"] = True
    return out
