import json
import math
import os

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


def compact(d: dict) -> str:
    """The report as Claude receives it: JSON without the whitespace FastMCP would indent with."""
    return json.dumps(d, separators=(",", ":"), ensure_ascii=False)


def factor_base(result: dict) -> dict:
    """Name the claims' shared folder once as `base`, and each claim's file relative to it."""
    claims = result.get("claims")
    if not isinstance(claims, list) or len(claims) < 2:
        return result
    base = os.path.commonpath([os.path.dirname(c["file"]) for c in claims])
    if base in ("", os.sep):
        return result
    out = dict(result)
    out["base"] = base
    out["claims"] = [{**c, "file": os.path.relpath(c["file"], base)} for c in claims]
    return out
