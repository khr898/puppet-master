"""Rank blocks of source against a question with BM25, so Flint sees only the relevant parts.

BM25 scores a block as a sparse vector of its terms: each query term adds its inverse document
frequency, weighted by how often the term occurs in the block and damped by block length. It
needs no model and no memory beyond the text, and it suits code, where the question usually
names the identifiers it is about.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass

K1 = 1.2
B = 0.75
# Score multiplier for a block that defines a name the query mentions: the definition of
# `est_tokens` should outrank the dozen places that call it.
DEFINES_BOOST = 2.0
# Longest block the reader gets, in lines; longer top-level blocks are split.
READ_BLOCK_LINES = 60

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
_PART = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_CLOSER = re.compile(r"^[)\]}]")
# A top-level name a block defines: `def f`, `class C`, `NAME =`, `name: T =`.
_DEFINES = re.compile(
    r"^(?:async\s+def|def|class)\s+(\w+)|^([A-Za-z_]\w*)\s*(?::[^=]*)?=(?!=)", re.M
)
STOPWORDS = frozenset(
    "a an and are as at be by do does for from how i if in is it of on or so that the this to "
    "what when where which who why with you your can should would will was were".split()
)


def terms(text: str) -> list[str]:
    """Search terms: each identifier, plus its snake_case and camelCase parts, lowercased."""
    out = []
    for word in _WORD.findall(text):
        low = word.lower()
        if low in STOPWORDS:
            continue
        out.append(low)
        parts = [p.lower() for p in _PART.findall(word)]
        if len(parts) > 1:
            out.extend(p for p in parts if p not in STOPWORDS)
    return out


@dataclass(frozen=True)
class Block:
    path: str
    start: int  # true line number of the first line
    lines: list[str]

    @property
    def end(self) -> int:
        return self.start + len(self.lines) - 1


def split_blocks(
    path: str, first: int, lines: list[str], max_lines: int | None = None
) -> list[Block]:
    """Split a file into top-level blocks: a new one starts at an unindented line after a blank.

    Comments and decorators directly above a definition stay with it. With max_lines, a block
    longer than that is cut into pieces of at most max_lines.
    """
    starts = [0]
    for i in range(1, len(lines)):
        line = lines[i]
        if line[:1] in ("", " ", "\t") or _CLOSER.match(line):
            continue
        if not lines[i - 1].strip():
            starts.append(i)
    blocks = []
    for a, b in zip(starts, [*starts[1:], len(lines)], strict=True):
        step = max_lines or (b - a) or 1
        for s in range(a, b, step):
            piece = lines[s : min(b, s + step)]
            if piece:
                blocks.append(Block(path, first + s, piece))
    return blocks


def defines(block: Block) -> set[str]:
    return {a or b for a, b in _DEFINES.findall("\n".join(block.lines))}


def with_definitions(ranked: list[Block], blocks: list[Block], hops: int = 3) -> list[Block]:
    """Ranked blocks where each of the first `hops` is followed by the same-file blocks that
    define the names it uses, so a function arrives with the constants and helpers it relies on.
    """
    index: dict[tuple[str, str], Block] = {}
    for b in blocks:
        for name in defines(b):
            index.setdefault((b.path, name), b)
    out: list[Block] = []
    seen: set[int] = set()

    def add(b: Block) -> None:
        if id(b) not in seen:
            seen.add(id(b))
            out.append(b)

    for i, block in enumerate(ranked):
        add(block)
        if i < hops:
            own = defines(block)
            for word in dict.fromkeys(_WORD.findall("\n".join(block.lines))):
                dep = index.get((block.path, word))
                if dep is not None and word not in own:
                    add(dep)
    return out


def bm25(query: str, docs: list[list[str]]) -> list[float]:
    """BM25 score of every doc (a list of terms) against the query."""
    q = set(terms(query))
    n = len(docs)
    if not n or not q:
        return [0.0] * n
    avg = sum(len(d) for d in docs) / n or 1.0
    df = Counter(t for d in docs for t in set(d) if t in q)
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in df}
    scores = []
    for d in docs:
        tf = Counter(t for t in d if t in idf)
        norm = K1 * (1 - B + B * len(d) / avg)
        scores.append(sum(idf[t] * f * (K1 + 1) / (f + norm) for t, f in tf.items()))
    return scores


def rank(query: str, blocks: list[Block]) -> list[tuple[float, Block]]:
    """Blocks with their scores, best first. The file path counts as part of every block, and
    a block defining a name the query mentions scores DEFINES_BOOST times higher.
    """
    docs = [terms(b.path) + terms("\n".join(b.lines)) for b in blocks]
    names = {w.lower() for w in _WORD.findall(query)}
    scores = [
        s * DEFINES_BOOST if names & {d.lower() for d in defines(b)} else s
        for s, b in zip(bm25(query, docs), blocks, strict=True)
    ]
    scored = zip(scores, range(len(blocks)), strict=True)
    return [(s, blocks[i]) for s, i in sorted(scored, key=lambda x: (-x[0], x[1]))]
