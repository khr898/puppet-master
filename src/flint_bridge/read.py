import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from flint_bridge.budget import HEADROOM, PathError, expand, fits
from flint_bridge.config import Config
from flint_bridge.llm import complete
from flint_bridge.report import est_tokens
from flint_bridge.retrieve import READ_BLOCK_LINES, Block, rank, split_blocks, with_definitions

READ_MAX_TOKENS = 1500
# Most file text a focused read will load and rank (~2 MB).
FOCUS_MAX_TOKENS = 500_000
# A focused read drops blocks scoring under this share of the best one: weak matches only
# distract Flint from the blocks that hold the answer.
READ_MIN_SHARE = 0.2
# Shown in place of each run of lines a focused read left out.
GAP = "  ...| (lines not shown)"
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

SYSTEM_MERGE = (
    "Combine partial answers into one answer to the question. You may only rely on the listed "
    'claims. Reply with JSON only: {"answer": string, "claim_ids": [string]}.'
)

MERGE_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "claim_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "claim_ids"],
}

# path -> (number of the first loaded line, loaded lines). A focused read leaves out lines it
# did not pick; those are None and are never shown to Flint or accepted as a citation.
Files = dict[str, tuple[int, list[str | None]]]


@dataclass(frozen=True)
class Claim:
    text: str
    file: str
    line: int
    quote: str


def number_lines(lines: list[str | None], start: int = 1) -> str:
    """Lines with their true numbers in the margin. A run of None lines becomes one gap marker."""
    out = []
    for n, line in enumerate(lines, start):
        if line is not None:
            out.append(f"{n:>5}| {line}")
        elif n == start or lines[n - start - 1] is not None:
            out.append(GAP)
    return "\n".join(out)


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
    if not 0 <= idx < len(lines) or lines[idx] is None:
        return False
    quote = _norm(claim.quote)
    if not quote:
        return False
    lo = max(0, idx - window)
    hi = min(len(lines), idx + window + 1)
    return quote in _norm(" ".join(x for x in lines[lo:hi] if x is not None))


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
    reasoning_tokens: int = 0
    model_seconds: float = 0.0
    # (system, user, raw output) of every model call, for sample capture
    calls: list[tuple[str, str, str]] = field(default_factory=list)


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
        started = time.perf_counter()
        reply = await complete(cfg, system, prompt, max_tokens=max_tokens, schema=schema)
        usage.model_seconds += time.perf_counter() - started
        usage.tokens_in += reply.tokens_in
        usage.tokens_out += reply.tokens_out
        usage.reasoning_tokens += reply.reasoning_tokens
        usage.calls.append((system, prompt, reply.raw))
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


def _line_chars(n: int, line: str) -> int:
    return len(f"{n:>5}| {line}") + 1


def chunk_files(files: Files, chunk_tokens: int) -> list[Files]:
    """Pack files (sorted by path) into chunks of at most chunk_tokens, as rendered.

    A file too big for one chunk is split on line boundaries and each piece keeps its true
    starting line number. A single line bigger than a chunk gets a chunk of its own; the
    caller's per-chunk budget check rejects it.
    """
    limit = chunk_tokens * 4
    chunks: list[Files] = []
    chunk: Files = {}
    used = 0
    for path, (first, lines) in sorted(files.items()):
        header = len(f"=== FILE {path} ===\n")
        if not lines:
            chunk[path] = (first, [])
            used += header
            continue
        start, piece = first, []
        for n, line in enumerate(lines, first):
            cost = _line_chars(n, line) + (0 if piece else header)
            if (chunk or piece) and used + cost > limit:
                if piece:
                    chunk[path] = (start, piece)
                chunks.append(chunk)
                chunk, used, piece = {}, 0, []
                cost = _line_chars(n, line) + header
            if not piece:
                start = n
            piece.append(line)
            used += cost
        chunk[path] = (start, piece)
    if chunk:
        chunks.append(chunk)
    return chunks


def _trim(block: Block) -> Block | None:
    """The block without its trailing blank lines, or None if it is all blank."""
    lines = list(block.lines)
    while lines and not lines[-1].strip():
        lines.pop()
    return Block(block.path, block.start, lines) if lines else None


def focus_files(files: Files, question: str, chunk_tokens: int) -> tuple[Files, dict] | None:
    """The blocks that best match the question (BM25), packed into one chunk.

    The best few come with the blocks that define the names they use.

    Returns (files holding only the picked lines, a coverage summary), or None when no block
    shares a term with the question.
    """
    blocks = [
        trimmed
        for path, (first, lines) in files.items()
        for b in split_blocks(path, first, lines, READ_BLOCK_LINES)
        if (trimmed := _trim(b)) is not None
    ]
    limit = chunk_tokens * 4
    used, picked, paths = 0, [], set()
    ranked = rank(question, blocks)
    floor = READ_MIN_SHARE * ranked[0][0] if ranked else 0
    relevant = [b for score, b in ranked if score > 0 and score >= floor]
    for block in with_definitions(relevant, blocks):
        cost = sum(_line_chars(n, x) for n, x in enumerate(block.lines, block.start))
        cost += len(GAP) + 1 + (0 if block.path in paths else len(f"=== FILE {block.path} ===\n"))
        if used + cost > limit:
            continue
        used += cost
        picked.append(block)
        paths.add(block.path)
    if not picked:
        return None
    out: Files = {}
    for path in paths:
        mine = [b for b in picked if b.path == path]
        lo, hi = min(b.start for b in mine), max(b.end for b in mine)
        lines: list[str | None] = [None] * (hi - lo + 1)
        for b in mine:
            lines[b.start - lo : b.end - lo + 1] = b.lines
        out[path] = (lo, lines)
    coverage = {
        "blocks": len(picked),
        "of_blocks": len(blocks),
        "shown_tokens": est_tokens(_render(out)),
        "total_tokens": est_tokens(_render(files)),
    }
    return out, coverage


def _read_prompt(chunk: Files, question: str) -> str:
    return f"{_render(chunk)}\nQUESTION: {question}"


def _valid_merge(data: dict) -> bool:
    ids = data.get("claim_ids")
    return (
        isinstance(data.get("answer"), str)
        and isinstance(ids, list)
        and all(isinstance(i, str) for i in ids)
    )


def _merge_prompt(question: str, answers: list[str], claims: dict[str, Claim]) -> str:
    partials = "\n".join(f"{i}. {a}" for i, a in enumerate(answers, 1))
    listed = "\n".join(f"{cid}: {c.text} ({c.file}:{c.line})" for cid, c in claims.items())
    return f"QUESTION: {question}\n\nPARTIAL ANSWERS:\n{partials}\n\nCLAIMS:\n{listed}"


def _claim_report(claims: list[Claim]) -> list[dict]:
    return [{"text": c.text, "file": c.file, "line": c.line} for c in claims]


async def flint_read_impl(
    cfg: Config,
    paths: list[str],
    question: str,
    *,
    usage: Usage | None = None,
    ready: Callable[[], Awaitable[dict | None]] | None = None,
    focus: bool = False,
) -> dict:
    """Read files through Flint and return only claims that were verified against the source.

    Files are split into chunks; each chunk is read on its own (map) and, when there is more
    than one, the verified claims are combined by a merge step that may only pick among them.
    With `focus`, files that need more than one chunk are cut down instead to the blocks that
    best match the question, in a single chunk, and the report says how much was shown.

    `ready` runs after every check that needs no model (so a bad request never starts Flint)
    and just before the first model call; an error dict it returns is handed straight back.
    """
    usage = usage if usage is not None else Usage()
    try:
        files = _load_files(paths)
    except PathError as e:
        return e.result
    if not files:
        return {"error": "no_files"}
    chunks = chunk_files(files, cfg.effective_chunk_tokens)
    max_out = READ_MAX_TOKENS + cfg.reasoning_reserve
    coverage = None
    if focus and len(chunks) > 1:
        total = est_tokens(_render(files))
        if total > FOCUS_MAX_TOKENS:
            return _over_budget(total, FOCUS_MAX_TOKENS)
        budget = min(cfg.focus_tokens, cfg.effective_chunk_tokens)
        focused = focus_files(files, question, budget)
        if focused is not None:
            files, coverage = focused
            chunks = [files]
    if len(chunks) > cfg.max_chunks:
        return _over_budget(est_tokens(_render(files)), cfg.effective_chunk_tokens * cfg.max_chunks)
    prompts = []
    for chunk in chunks:
        tokens = est_tokens(_render(chunk))
        if tokens > cfg.effective_chunk_tokens:  # a single line bigger than a chunk
            return _over_budget(tokens, cfg.effective_chunk_tokens)
        prompt = _read_prompt(chunk, question)
        prompt_tokens = est_tokens(SYSTEM_READ + prompt)
        if not fits(prompt_tokens, max_out, cfg):
            return _over_budget(prompt_tokens, cfg.ctx - HEADROOM - max_out)
        prompts.append(prompt)
    if ready is not None and (err := await ready()) is not None:
        return err

    answers: list[str] = []
    claims: dict[str, Claim] = {}
    stripped = 0
    for chunk, prompt in zip(chunks, prompts, strict=True):
        data = await _complete_json(
            cfg, SYSTEM_READ, prompt, READ_SCHEMA, max_out, usage, _valid_read
        )
        if data is None:
            return {"error": "unverifiable_output"}
        parsed, malformed = parse_claims(data["claims"])
        verified, unverified = filter_claims(parsed, chunk)
        stripped += malformed + unverified
        answers.append(data["answer"])
        claims.update({f"c{len(claims) + i}": c for i, c in enumerate(verified, 1)})

    if len(chunks) == 1:
        answer, chosen = answers[0], list(claims.values())
    else:
        merge_prompt = _merge_prompt(question, answers, claims)
        merge_tokens = est_tokens(SYSTEM_MERGE + merge_prompt)
        if not fits(merge_tokens, max_out, cfg):
            return _over_budget(merge_tokens, cfg.ctx - HEADROOM - max_out)
        merged = await _complete_json(
            cfg, SYSTEM_MERGE, merge_prompt, MERGE_SCHEMA, max_out, usage, _valid_merge
        )
        if merged is None:
            return {"error": "unverifiable_output"}
        answer = merged["answer"]
        picked = dict.fromkeys(i for i in merged["claim_ids"] if i in claims)
        chosen = [claims[i] for i in picked]

    out = {
        "answer": answer,
        "claims": _claim_report(chosen),
        "stripped": stripped,
        "chunks": len(chunks),
    }
    if coverage is not None:
        out["focus"] = coverage
    if stripped:
        out["answer_confidence"] = "degraded"
    return out
