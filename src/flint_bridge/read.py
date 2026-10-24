from dataclasses import dataclass


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


def verify_claim(claim: Claim, files: dict[str, list[str]], window: int = 2) -> bool:
    """True if claim.quote appears within ±window lines of claim.line in claim.file."""
    lines = files.get(claim.file)
    if lines is None or not 1 <= claim.line <= len(lines):
        return False
    quote = _norm(claim.quote)
    if not quote:
        return False
    lo = max(0, claim.line - 1 - window)
    hi = min(len(lines), claim.line + window)
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


def filter_claims(claims: list[Claim], files: dict[str, list[str]]) -> tuple[list[Claim], int]:
    """Keep only claims whose quote is found in the source. Returns (verified, stripped)."""
    verified = [c for c in claims if verify_claim(c, files)]
    return verified, len(claims) - len(verified)
