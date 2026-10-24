import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from flint_bridge.budget import PathError, expand, fits
from flint_bridge.config import Config
from flint_bridge.llm import complete
from flint_bridge.report import est_tokens

READ_MAX_TOKENS = 1500
RETRY_NOTE = "Your last reply was not valid JSON. Reply with JSON only."

SYSTEM_READ = (
    "You answer questions about source files. Every factual statement must be backed by a "
    "claim citing the exact file path, the line number shown in the left margin, and a short "
    "quote copied character-for-character from that line. Never cite lines you were not shown. "
    'Reply with JSON only: {"answer": string, "claims": [{"text": string, "file": string, '
    '"line": int, "quote": string}]}.'
)

READ_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "quote": {"type": "string"},
                },
                "required": ["text", "file", "line", "quote"],
            },
        },
    },
    "required": ["answer", "claims"],
}

Files = dict[str, tuple[int, list[str]]]


@dataclass(frozen=True)
class Claim:
    text: str
    file: str
    line: int
    quote: str


def number_lines(lines: list[str], start: int = 1) -> str:
    return "\n".join(f"{n:>5}| {line}" for n, line in enumerate(lines, start))


def _norm(s: str) -> str:
    return " ".join(s.split())


def verify_claim(claim: Claim, files: dict[str, tuple[int, list[str]]], window: int = 2) -> bool:
    """True if claim.quote appears within ±window lines of claim.line in claim.file.

    files maps path -> (number of the first loaded line, loaded lines), so a slice
    like src/x.py:10-20 is checked against its original line numbers.
    """
    loaded = files.get(claim.file)
    if loaded is None:
        return False
    start, lines = loaded
    idx = claim.line - start
    if not 0 <= idx < len(lines):
        return False
    quote = _norm(claim.quote)
    if not quote:
        return False
    lo = max(0, idx - window)
    hi = min(len(lines), idx + window + 1)
    return quote in _norm(" ".join(lines[lo:hi]))


def _parse_claim(item: object) -> Claim | None:
    if not isinstance(item, dict):
        return None
    text, file, quote, line = (item.get(k) for k in ("text", "file", "quote", "line"))
    if not all(isinstance(v, str) for v in (text, file, quote)):
        return None
    if isinstance(line, bool):
        return None
    if isinstance(line, str):
        try:
            line = int(line)
        except ValueError:
            return None
    if not isinstance(line, int):
        return None
    return Claim(text, file, line, quote)


def parse_claims(raw: list) -> tuple[list[Claim], int]:
    """Coerce raw JSON claims into Claim objects. Returns (valid, malformed_count)."""
    parsed = [_parse_claim(item) for item in raw]
    valid = [c for c in parsed if c is not None]
    return valid, len(parsed) - len(valid)


def filter_claims(
    claims: list[Claim], files: dict[str, tuple[int, list[str]]]
) -> tuple[list[Claim], int]:
    """Keep only claims whose quote is found in the source. Returns (verified, stripped)."""
    verified = [c for c in claims if verify_claim(c, files)]
    return verified, len(claims) - len(verified)


@dataclass
class Usage:
    """Flint token use and retries across one tool call, for the call log."""

    tokens_in: int = 0
    tokens_out: int = 0
    retries: int = 0


async def _complete_json(
    cfg: Config,
    system: str,
    user: str,
    schema: dict,
    max_tokens: int,
    usage: Usage,
    valid: Callable[[dict], bool],
) -> dict | None:
    """Ask for schema-constrained JSON; retry once if it doesn't parse or validate."""
    for attempt in range(2):
        if attempt:
            usage.retries += 1
        prompt = f"{user}\n\n{RETRY_NOTE}" if attempt else user
        reply = await complete(cfg, system, prompt, max_tokens=max_tokens, schema=schema)
        usage.tokens_in += reply.tokens_in
        usage.tokens_out += reply.tokens_out
        try:
            data = json.loads(reply.text)
        except ValueError:
            continue
        if isinstance(data, dict) and valid(data):
            return data
    return None


def _load_files(paths: list[str]) -> Files:
    """Read the files (and line ranges) as UTF-8. Raises PathError."""
    files: Files = {}
    for path, start, end in expand(paths):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as e:
            raise PathError({"error": "unreadable", "path": str(path)}) from e
        first = 1
        if start is not None:
            if start < 1 or end < start or start > len(lines):
                raise PathError(
                    {"error": "bad_range", "path": str(path), "range": f"{start}-{end}"}
                )
            lines, first = lines[start - 1 : end], start
        files.setdefault(str(path), (first, lines))
    return files


def _render(files: Files) -> str:
    """Files in sorted order, numbered with their true line numbers. The question goes after."""
    return "".join(
        f"=== FILE {path} ===\n{number_lines(lines, first)}\n"
        for path, (first, lines) in sorted(files.items())
    )


def _over_budget(tokens: int, limit: int) -> dict:
    return {
        "error": "over_budget",
        "est_tokens": tokens,
        "limit": limit,
        "hint": "narrow paths or use path:start-end",
    }


def _valid_read(data: dict) -> bool:
    return isinstance(data.get("answer"), str) and isinstance(data.get("claims"), list)


async def flint_read_impl(
    cfg: Config,
    paths: list[str],
    question: str,
    *,
    usage: Usage | None = None,
    ready: Callable[[], Awaitable[dict | None]] | None = None,
) -> dict:
    """Read files through Flint and return only claims that were verified against the source.

    `ready` runs after every check that needs no model (so a bad request never starts Flint)
    and just before the first model call; an error dict it returns is handed straight back.
    """
    usage = usage if usage is not None else Usage()
    try:
        files = _load_files(paths)
    except PathError as e:
        return e.result
    body = _render(files)
    tokens = est_tokens(body)
    if tokens > cfg.chunk_tokens:
        return _over_budget(tokens, cfg.chunk_tokens)
    user = f"{body}\nQUESTION: {question}"
    prompt_tokens = est_tokens(SYSTEM_READ + user)
    if not fits(prompt_tokens, READ_MAX_TOKENS, cfg):
        return _over_budget(prompt_tokens, cfg.ctx - 512 - READ_MAX_TOKENS)
    if ready is not None and (err := await ready()) is not None:
        return err
    data = await _complete_json(
        cfg, SYSTEM_READ, user, READ_SCHEMA, READ_MAX_TOKENS, usage, _valid_read
    )
    if data is None:
        return {"error": "unverifiable_output"}
    claims, malformed = parse_claims(data["claims"])
    verified, unverified = filter_claims(claims, files)
    stripped = malformed + unverified
    out = {
        "answer": data["answer"],
        "claims": [{"text": c.text, "file": c.file, "line": c.line} for c in verified],
        "stripped": stripped,
        "chunks": 1,
    }
    if stripped:
        out["answer_confidence"] = "degraded"
    return out
